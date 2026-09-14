"""Commodity futures sizing: stop-distance risk with margin preview."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from trading.broker.ports import (
    MarginPreviewLeg,
    MarginPreviewPort,
    MarginPreviewRequest,
)
from trading.config.risk_policy import RiskPolicyConfig
from trading.config.schema import RiskLimits
from trading.domain.contracts.common import ContractRef
from trading.domain.contracts.instrument import InstrumentSpec
from trading.domain.contracts.intent import TradeIntent
from trading.domain.contracts.portfolio import PortfolioSnapshot
from trading.domain.contracts.sizing import SizingRequest
from trading.domain.contracts.snapshot import FeatureSnapshot
from trading.domain.enums import AssetClass, InstrumentKind, Side
from trading.domain.primitives import Lots, LotSize, Money, Percent, Price, Rounding
from trading.risk.limits import (
    floor_divide_money,
    open_trade_slots,
    underlying_exposure_for,
)
from trading.risk.sizing.long_option import LotBounds

__all__ = [
    "CommodityFutureSizingEngine",
    "CommodityFutureSizingResult",
    "is_commodity_future",
]


@dataclass(frozen=True, slots=True)
class CommodityFutureSizingResult:
    """Sized outcome for a directional commodity futures entry."""

    bounds: LotBounds
    approved_lots: int
    cost_per_lot: Money
    estimated_margin: Money
    recalculated_max_loss: Money
    entry_price: Price
    lot_size: LotSize
    stop_distance_ticks: int
    margin_per_lot: Money


def is_commodity_future(
    intent: TradeIntent,
    instrument: InstrumentSpec,
) -> bool:
    """Return True for a single-leg commodity future, in either direction.

    This detects the *structure*, not the admissibility of it. A short future is
    still a commodity future, so it must be classified as one: the naked-short
    policy in the risk gateway then refuses it with an accurate reason code,
    instead of it falling through to "unknown instrument" and looking like a bad
    symbol. Pricing a short is a separate concern, guarded in ``size`` below.
    """
    if len(intent.legs) != 1:
        return False
    leg = intent.legs[0]
    return (
        instrument.instrument_kind is InstrumentKind.FUTURE
        and intent.asset_class is AssetClass.COMMODITY
        and leg.contract.instrument_kind is InstrumentKind.FUTURE
    )


class CommodityFutureSizingEngine:
    """Size futures from stop-distance risk and broker margin preview."""

    def size(
        self,
        request: SizingRequest,
        instrument: InstrumentSpec,
        policy: RiskPolicyConfig,
        margin_preview: MarginPreviewPort,
        *,
        account_id: str,
        account_risk: RiskLimits,
        preview_request_id: str,
    ) -> CommodityFutureSizingResult:
        """Compute lots from stop distance, lot value and the min() formula."""
        if not is_commodity_future(request.intent, instrument):
            raise ValueError("intent is not a commodity future entry")
        if request.intent.legs[0].side is not Side.BUY:
            raise ValueError(
                "this engine prices long futures only: the margin preview below "
                "requests a BUY, so sizing a short against long margin would "
                "understate it. The naked-short policy refuses that direction."
            )
        intent = request.intent
        leg = intent.legs[0]
        feature = request.feature_snapshot
        entry_price = self._entry_price(feature)
        lot_size = LotSize(instrument.lot_size)
        stop_distance_ticks = intent.exit_template.stop_distance_ticks
        tick = entry_price.tick
        currency = request.limits.max_loss_per_trade.currency
        one_lot = Lots(1).to_quantity(lot_size)
        risk_per_contract = Decimal(stop_distance_ticks) * tick.value
        risk_per_lot = Money(risk_per_contract * one_lot.contracts, currency)
        slippage = Percent.from_fraction(policy.slippage_buffer_fraction).of(
            risk_per_lot
        )
        charges = policy.charges_per_lot.to_money()
        cost_per_lot = (risk_per_lot + slippage + charges).quantized(Rounding.CEILING)

        risk_lots = floor_divide_money(request.limits.max_loss_per_trade, cost_per_lot)
        capital_budget = min(
            request.limits.strategy_allocation_remaining,
            request.limits.margin_available,
            request.limits.daily_loss_remaining,
            key=lambda money: money.amount,
        )
        capital_lots = floor_divide_money(capital_budget, cost_per_lot)
        margin_lots, margin_per_lot = self._margin_lots(
            margin_preview,
            account_id=account_id,
            preview_request_id=preview_request_id,
            contract=leg.contract,
            lot_size=lot_size,
            margin_available=request.limits.margin_available,
        )
        portfolio_limit_lots = self._portfolio_limit_lots(
            request,
            policy,
            cost_per_lot,
            risk_per_lot,
            account_risk=account_risk,
            feature=feature,
            lot_size=lot_size,
        )
        liquidity_lots = self._liquidity_lots(feature, lot_size)

        bounds = LotBounds(
            risk_lots=risk_lots,
            capital_lots=capital_lots,
            margin_lots=margin_lots,
            portfolio_limit_lots=portfolio_limit_lots,
            liquidity_lots=liquidity_lots,
        )
        approved_lots = bounds.approved_lots
        recalculated_max_loss = (cost_per_lot * approved_lots).quantized(
            Rounding.CEILING
        )
        if approved_lots > 0:
            estimated_margin = (margin_per_lot * approved_lots).quantized(
                Rounding.CEILING
            )
        else:
            estimated_margin = Money.zero(currency)

        return CommodityFutureSizingResult(
            bounds=bounds,
            approved_lots=approved_lots,
            cost_per_lot=cost_per_lot,
            estimated_margin=estimated_margin,
            recalculated_max_loss=recalculated_max_loss,
            entry_price=entry_price,
            lot_size=lot_size,
            stop_distance_ticks=stop_distance_ticks,
            margin_per_lot=margin_per_lot,
        )

    @staticmethod
    def _entry_price(feature: FeatureSnapshot) -> Price:
        ask = feature.market.ask
        if ask is None:
            raise ValueError("ask price is required to size a futures entry")
        return ask

    @staticmethod
    def _margin_lots(
        margin_preview: MarginPreviewPort,
        *,
        account_id: str,
        preview_request_id: str,
        contract: ContractRef,
        lot_size: LotSize,
        margin_available: Money,
    ) -> tuple[int, Money]:
        quantity = Lots(1).to_quantity(lot_size)
        preview = margin_preview.preview_margin(
            MarginPreviewRequest(
                request_id=preview_request_id,
                account_id=account_id,
                legs=(
                    MarginPreviewLeg(
                        contract=contract,
                        side=Side.BUY,
                        quantity_contracts=quantity.contracts,
                    ),
                ),
            )
        )
        if not preview.confirmed or preview.margin_required.is_zero:
            return 0, Money.zero(margin_available.currency)
        margin_per_lot = preview.margin_required.quantized(Rounding.CEILING)
        lots = floor_divide_money(margin_available, margin_per_lot)
        return lots, margin_per_lot

    def _portfolio_limit_lots(
        self,
        request: SizingRequest,
        policy: RiskPolicyConfig,
        cost_per_lot: Money,
        notional_per_lot: Money,
        *,
        account_risk: RiskLimits,
        feature: FeatureSnapshot,
        lot_size: LotSize,
    ) -> int:
        portfolio = request.portfolio_snapshot
        equity = portfolio.exposure.equity
        slots = open_trade_slots(portfolio, account_risk)
        if slots <= 0:
            return 0

        concentration_cap = (
            equity * policy.underlying_concentration_fraction
        ).quantized(Rounding.FLOOR)
        current = underlying_exposure_for(portfolio, request.intent.underlying)
        if current is not None:
            current_notional = current.gross_notional
        else:
            current_notional = Money.zero(equity.currency)
        concentration_remaining = concentration_cap - current_notional
        concentration_lots = floor_divide_money(
            concentration_remaining, notional_per_lot
        )

        delta_lots = self._delta_limit_lots(
            policy,
            feature=feature,
            portfolio=portfolio,
            lot_size=lot_size,
        )
        return min(concentration_lots, delta_lots, slots)

    @staticmethod
    def _delta_limit_lots(
        policy: RiskPolicyConfig,
        *,
        feature: FeatureSnapshot,
        portfolio: PortfolioSnapshot,
        lot_size: LotSize,
    ) -> int:
        derivatives = feature.derivatives
        if derivatives is None or derivatives.greeks is None:
            return 10_000
        delta = derivatives.greeks.delta
        if delta is None or delta == 0:
            return 10_000
        current_delta = int(portfolio.exposure.net_delta)
        headroom = policy.net_delta_limit - abs(current_delta)
        delta_per_lot = abs(int(delta * Decimal(lot_size.contracts_per_lot)))
        if delta_per_lot <= 0:
            return 10_000
        return max(headroom // delta_per_lot, 0)

    @staticmethod
    def _liquidity_lots(feature: FeatureSnapshot, lot_size: LotSize) -> int:
        bid_size = feature.market.bid_size
        ask_size = feature.market.ask_size
        if bid_size is None or ask_size is None:
            return 10_000
        depth = min(bid_size, ask_size)
        return max(depth // lot_size.contracts_per_lot, 0)
