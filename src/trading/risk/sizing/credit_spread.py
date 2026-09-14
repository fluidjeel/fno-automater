"""Credit spread sizing: net credit with defined maximum loss."""

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
    "CreditSpreadSizingEngine",
    "CreditSpreadSizingResult",
    "credit_spread_legs",
    "is_credit_spread",
]


@dataclass(frozen=True, slots=True)
class CreditSpreadSizingResult:
    """Sized outcome for a two-leg credit spread."""

    bounds: LotBounds
    approved_lots: int
    cost_per_lot: Money
    estimated_margin: Money
    recalculated_max_loss: Money
    net_credit_per_unit: Price
    lot_size: LotSize
    short_leg: IntentLeg
    long_leg: IntentLeg


_CREDIT_SPREAD_LEG_COUNT = 2


def is_credit_spread(intent: TradeIntent) -> bool:
    """Return True when the intent expresses a two-leg defined-risk credit spread."""
    legs = intent.legs
    contracts = [leg.contract for leg in legs]
    buy = next((leg for leg in legs if leg.side is Side.BUY), None)
    sell = next((leg for leg in legs if leg.side is Side.SELL), None)
    if buy is None or sell is None:
        return False
    buy_strike = buy.contract.strike
    sell_strike = sell.contract.strike
    option_type = sell.contract.option_type
    return (
        len(legs) == _CREDIT_SPREAD_LEG_COUNT
        and all(
            contract.instrument_kind is InstrumentKind.OPTION for contract in contracts
        )
        and contracts[0].expiry == contracts[1].expiry
        and contracts[0].option_type == contracts[1].option_type
        and buy_strike is not None
        and sell_strike is not None
        and buy_strike != sell_strike
        and (
            (option_type is OptionType.CALL and sell_strike < buy_strike)
            or (option_type is OptionType.PUT and sell_strike > buy_strike)
        )
    )


def credit_spread_legs(intent: TradeIntent) -> tuple[IntentLeg, IntentLeg]:
    """Return the short and long legs for a validated credit spread intent."""
    if not is_credit_spread(intent):
        raise ValueError("intent is not a credit spread")
    short_leg = next(leg for leg in intent.legs if leg.side is Side.SELL)
    long_leg = next(leg for leg in intent.legs if leg.side is Side.BUY)
    return short_leg, long_leg


class CreditSpreadSizingEngine:
    """Size net-credit option spreads with defined maximum loss."""

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
    ) -> CreditSpreadSizingResult:
        """Compute lots from net credit and the min() sizing formula."""
        intent = request.intent
        short_leg, long_leg = credit_spread_legs(intent)
        short_feature = _leg_snapshot(leg_snapshots, short_leg.leg_id)
        long_feature = _leg_snapshot(leg_snapshots, long_leg.leg_id)
        net_credit_per_unit = _net_credit_per_unit(short_feature, long_feature)
        currency = request.limits.max_loss_per_trade.currency
        one_lot = Lots(1).to_quantity(lot_size)
        max_loss_per_unit = _max_loss_per_unit(short_leg, long_leg, net_credit_per_unit)
        loss_per_lot = max_loss_per_unit.notional(one_lot, currency)
        slippage = Percent.from_fraction(policy.slippage_buffer_fraction).of(
            loss_per_lot
        )
        charges = policy.charges_per_lot.to_money()
        cost_per_lot = (loss_per_lot + slippage + charges).quantized(Rounding.CEILING)

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
            short_leg=short_leg,
            long_leg=long_leg,
            lot_size=lot_size,
            margin_available=request.limits.margin_available,
        )
        portfolio_limit_lots = self._portfolio_limit_lots(
            request,
            policy,
            cost_per_lot,
            loss_per_lot,
            account_risk=account_risk,
            short_feature=short_feature,
            long_feature=long_feature,
            lot_size=lot_size,
        )
        liquidity_lots = self._liquidity_lots(short_feature, long_feature, lot_size)

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

        return CreditSpreadSizingResult(
            bounds=bounds,
            approved_lots=approved_lots,
            cost_per_lot=cost_per_lot,
            estimated_margin=estimated_margin,
            recalculated_max_loss=recalculated_max_loss,
            net_credit_per_unit=net_credit_per_unit,
            lot_size=lot_size,
            short_leg=short_leg,
            long_leg=long_leg,
        )

    @staticmethod
    def _margin_lots(
        margin_preview: MarginPreviewPort,
        *,
        account_id: str,
        preview_request_id: str,
        short_leg: IntentLeg,
        long_leg: IntentLeg,
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
                        contract=short_leg.contract,
                        side=Side.SELL,
                        quantity_contracts=quantity.contracts,
                    ),
                    MarginPreviewLeg(
                        contract=long_leg.contract,
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
        loss_per_lot: Money,
        *,
        account_risk: RiskLimits,
        short_feature: FeatureSnapshot,
        long_feature: FeatureSnapshot,
        lot_size: LotSize,
    ) -> int:
        portfolio = request.portfolio_snapshot
        equity = portfolio.exposure.equity
        slots = open_trade_slots(portfolio, account_risk)
        if slots <= 0:
            return 0

        premium_budget = (equity * policy.options_premium_budget_fraction).quantized(
            Rounding.FLOOR
        )
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
        concentration_lots = floor_divide_money(concentration_remaining, loss_per_lot)

        delta_lots = self._delta_limit_lots(
            policy,
            short_feature=short_feature,
            long_feature=long_feature,
            portfolio=portfolio,
            lot_size=lot_size,
        )
        return min(premium_lots, concentration_lots, delta_lots, slots)

    @staticmethod
    def _delta_limit_lots(
        policy: RiskPolicyConfig,
        *,
        short_feature: FeatureSnapshot,
        long_feature: FeatureSnapshot,
        portfolio: PortfolioSnapshot,
        lot_size: LotSize,
    ) -> int:
        short_delta = _leg_delta(short_feature)
        long_delta = _leg_delta(long_feature)
        if short_delta is None or long_delta is None:
            return 10_000
        net_delta_per_lot = abs(
            int((short_delta + long_delta) * Decimal(lot_size.contracts_per_lot))
        )
        if net_delta_per_lot <= 0:
            return 10_000
        current_delta = int(portfolio.exposure.net_delta)
        headroom = policy.net_delta_limit - abs(current_delta)
        return max(headroom // net_delta_per_lot, 0)

    @staticmethod
    def _liquidity_lots(
        short_feature: FeatureSnapshot,
        long_feature: FeatureSnapshot,
        lot_size: LotSize,
    ) -> int:
        short_depth = _leg_depth(short_feature)
        long_depth = _leg_depth(long_feature)
        if short_depth is None or long_depth is None:
            return 10_000
        depth = min(short_depth, long_depth)
        return max(depth // lot_size.contracts_per_lot, 0)


def _leg_snapshot(
    leg_snapshots: Mapping[str, FeatureSnapshot],
    leg_id: str,
) -> FeatureSnapshot:
    snapshot = leg_snapshots.get(leg_id)
    if snapshot is None:
        raise ValueError(f"missing feature snapshot for leg {leg_id}")
    return snapshot


def _net_credit_per_unit(
    short_feature: FeatureSnapshot,
    long_feature: FeatureSnapshot,
) -> Price:
    short_bid = short_feature.market.bid
    long_ask = long_feature.market.ask
    if short_bid is None or long_ask is None:
        raise ValueError("credit spread sizing requires short bid and long ask")
    net_value = short_bid.value - long_ask.value
    if net_value <= 0:
        raise ValueError("credit spread net credit must be positive")
    return Price(net_value, short_bid.tick)


def _max_loss_per_unit(
    short_leg: IntentLeg,
    long_leg: IntentLeg,
    net_credit_per_unit: Price,
) -> Price:
    short_strike = short_leg.contract.strike
    long_strike = long_leg.contract.strike
    if short_strike is None or long_strike is None:
        raise ValueError("credit spread legs require strikes")
    width = abs(long_strike - short_strike)
    max_loss_value = width - net_credit_per_unit.value
    if max_loss_value <= 0:
        raise ValueError("net credit exceeds spread width; max loss is undefined")
    return Price(max_loss_value, net_credit_per_unit.tick)


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
