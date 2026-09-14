"""Debit spread sizing: net debit with defined maximum loss."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

from trading.broker.ports import (
    MarginPreviewLeg,
    MarginPreviewPort,
    MarginPreviewRequest,
)
from trading.config.risk_policy import RiskPolicyConfig
from trading.config.schema import RiskLimits
from trading.domain.contracts.intent import IntentLeg, TradeIntent
from trading.domain.contracts.portfolio import PortfolioSnapshot
from trading.domain.contracts.sizing import SizingRequest
from trading.domain.contracts.snapshot import FeatureSnapshot
from trading.domain.enums import InstrumentKind, OptionType, Side
from trading.domain.primitives import Lots, LotSize, Money, Percent, Price, Rounding
from trading.risk.limits import (
    floor_divide_money,
    open_trade_slots,
    premium_budget_used,
    underlying_exposure_for,
)
from trading.risk.sizing.long_option import LotBounds

__all__ = [
    "DebitSpreadSizingEngine",
    "DebitSpreadSizingResult",
    "is_debit_spread",
    "spread_legs",
]


@dataclass(frozen=True, slots=True)
class DebitSpreadSizingResult:
    """Sized outcome for a two-leg debit spread."""

    bounds: LotBounds
    approved_lots: int
    cost_per_lot: Money
    estimated_margin: Money
    recalculated_max_loss: Money
    net_debit_per_unit: Price
    lot_size: LotSize
    long_leg: IntentLeg
    short_leg: IntentLeg


_DEBIT_SPREAD_LEG_COUNT = 2


def is_debit_spread(intent: TradeIntent) -> bool:
    """Return True when the intent expresses a two-leg defined-risk debit spread."""
    legs = intent.legs
    contracts = [leg.contract for leg in legs]
    buy = next((leg for leg in legs if leg.side is Side.BUY), None)
    sell = next((leg for leg in legs if leg.side is Side.SELL), None)
    if buy is None or sell is None:
        return False
    buy_strike = buy.contract.strike
    sell_strike = sell.contract.strike
    option_type = buy.contract.option_type
    return (
        len(legs) == _DEBIT_SPREAD_LEG_COUNT
        and all(
            contract.instrument_kind is InstrumentKind.OPTION for contract in contracts
        )
        and contracts[0].expiry == contracts[1].expiry
        and contracts[0].option_type == contracts[1].option_type
        and buy_strike is not None
        and sell_strike is not None
        and buy_strike != sell_strike
        and (
            (option_type is OptionType.CALL and buy_strike < sell_strike)
            or (option_type is OptionType.PUT and buy_strike > sell_strike)
        )
    )


def spread_legs(intent: TradeIntent) -> tuple[IntentLeg, IntentLeg]:
    """Return the long and short legs for a validated debit spread intent."""
    if not is_debit_spread(intent):
        raise ValueError("intent is not a debit spread")
    long_leg = next(leg for leg in intent.legs if leg.side is Side.BUY)
    short_leg = next(leg for leg in intent.legs if leg.side is Side.SELL)
    return long_leg, short_leg


class DebitSpreadSizingEngine:
    """Size net-debit option spreads with defined maximum loss."""

    def size(
        self,
        request: SizingRequest,
        leg_snapshots: Mapping[str, FeatureSnapshot],
        policy: RiskPolicyConfig,
        margin_preview: MarginPreviewPort,
        *,
        account_id: str,
        account_risk: RiskLimits,
        preview_request_id: str,
        lot_size: LotSize,
    ) -> DebitSpreadSizingResult:
        """Compute lots from net debit and the min() sizing formula."""
        intent = request.intent
        long_leg, short_leg = spread_legs(intent)
        long_feature = _leg_snapshot(leg_snapshots, long_leg.leg_id)
        short_feature = _leg_snapshot(leg_snapshots, short_leg.leg_id)
        net_debit_per_unit = _net_debit_per_unit(long_feature, short_feature)
        currency = request.limits.max_loss_per_trade.currency
        one_lot = Lots(1).to_quantity(lot_size)
        premium_per_lot = net_debit_per_unit.notional(one_lot, currency)
        slippage = Percent.from_fraction(policy.slippage_buffer_fraction).of(
            premium_per_lot
        )
        charges = policy.charges_per_lot.to_money()
        cost_per_lot = (premium_per_lot + slippage + charges).quantized(
            Rounding.CEILING
        )
        _validate_defined_risk(long_leg, short_leg, net_debit_per_unit)

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
            long_leg=long_leg,
            short_leg=short_leg,
            lot_size=lot_size,
            margin_available=request.limits.margin_available,
        )
        portfolio_limit_lots = self._portfolio_limit_lots(
            request,
            policy,
            cost_per_lot,
            premium_per_lot,
            account_risk=account_risk,
            long_feature=long_feature,
            short_feature=short_feature,
            lot_size=lot_size,
        )
        liquidity_lots = self._liquidity_lots(long_feature, short_feature, lot_size)

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

        return DebitSpreadSizingResult(
            bounds=bounds,
            approved_lots=approved_lots,
            cost_per_lot=cost_per_lot,
            estimated_margin=estimated_margin,
            recalculated_max_loss=recalculated_max_loss,
            net_debit_per_unit=net_debit_per_unit,
            lot_size=lot_size,
            long_leg=long_leg,
            short_leg=short_leg,
        )

    @staticmethod
    def _margin_lots(
        margin_preview: MarginPreviewPort,
        *,
        account_id: str,
        preview_request_id: str,
        long_leg: IntentLeg,
        short_leg: IntentLeg,
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
                        contract=long_leg.contract,
                        side=Side.BUY,
                        quantity_contracts=quantity.contracts,
                    ),
                    MarginPreviewLeg(
                        contract=short_leg.contract,
                        side=Side.SELL,
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
        premium_per_lot: Money,
        *,
        account_risk: RiskLimits,
        long_feature: FeatureSnapshot,
        short_feature: FeatureSnapshot,
        lot_size: LotSize,
    ) -> int:
        portfolio = request.portfolio_snapshot
        equity = portfolio.exposure.equity
        slots = open_trade_slots(portfolio, account_risk)
        if slots <= 0:
            return 0

        premium_budget = (
            equity * policy.options_premium_budget_fraction
        ).quantized(Rounding.FLOOR)
        premium_remaining = premium_budget - premium_budget_used(portfolio)
        premium_lots = floor_divide_money(premium_remaining, cost_per_lot)

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
            concentration_remaining,
            premium_per_lot,
        )

        delta_lots = self._delta_limit_lots(
            policy,
            long_feature=long_feature,
            short_feature=short_feature,
            portfolio=portfolio,
            lot_size=lot_size,
        )
        return min(premium_lots, concentration_lots, delta_lots, slots)

    @staticmethod
    def _delta_limit_lots(
        policy: RiskPolicyConfig,
        *,
        long_feature: FeatureSnapshot,
        short_feature: FeatureSnapshot,
        portfolio: PortfolioSnapshot,
        lot_size: LotSize,
    ) -> int:
        long_delta = _leg_delta(long_feature)
        short_delta = _leg_delta(short_feature)
        if long_delta is None or short_delta is None:
            return 10_000
        net_delta_per_lot = abs(
            int((long_delta - short_delta) * Decimal(lot_size.contracts_per_lot))
        )
        if net_delta_per_lot <= 0:
            return 10_000
        current_delta = int(portfolio.exposure.net_delta)
        headroom = policy.net_delta_limit - abs(current_delta)
        return max(headroom // net_delta_per_lot, 0)

    @staticmethod
    def _liquidity_lots(
        long_feature: FeatureSnapshot,
        short_feature: FeatureSnapshot,
        lot_size: LotSize,
    ) -> int:
        long_depth = _leg_depth(long_feature)
        short_depth = _leg_depth(short_feature)
        if long_depth is None or short_depth is None:
            return 10_000
        depth = min(long_depth, short_depth)
        return max(depth // lot_size.contracts_per_lot, 0)


def _leg_snapshot(
    leg_snapshots: Mapping[str, FeatureSnapshot],
    leg_id: str,
) -> FeatureSnapshot:
    snapshot = leg_snapshots.get(leg_id)
    if snapshot is None:
        raise ValueError(f"missing feature snapshot for leg {leg_id}")
    return snapshot


def _net_debit_per_unit(
    long_feature: FeatureSnapshot,
    short_feature: FeatureSnapshot,
) -> Price:
    long_ask = long_feature.market.ask
    short_bid = short_feature.market.bid
    if long_ask is None or short_bid is None:
        raise ValueError("debit spread sizing requires long ask and short bid")
    net_value = long_ask.value - short_bid.value
    if net_value <= 0:
        raise ValueError("debit spread net debit must be positive")
    return Price(net_value, long_ask.tick)


def _validate_defined_risk(
    long_leg: IntentLeg,
    short_leg: IntentLeg,
    net_debit_per_unit: Price,
) -> None:
    long_strike = long_leg.contract.strike
    short_strike = short_leg.contract.strike
    if long_strike is None or short_strike is None:
        raise ValueError("debit spread legs require strikes")
    width = abs(short_strike - long_strike)
    if net_debit_per_unit.value >= width:
        raise ValueError("net debit exceeds spread width; max loss is undefined")


def _leg_delta(feature: FeatureSnapshot) -> Decimal | None:
    derivatives = feature.derivatives
    if derivatives is None or derivatives.greeks is None:
        return None
    return derivatives.greeks.delta


def _leg_depth(feature: FeatureSnapshot) -> int | None:
    bid_size = feature.market.bid_size
    ask_size = feature.market.ask_size
    if bid_size is None or ask_size is None:
        return None
    return min(bid_size, ask_size)
