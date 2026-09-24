"""Long 1:2:1 butterfly sizing: net debit with bounded maximum loss."""

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
from trading.domain.contracts.sizing import SizingRequest
from trading.domain.contracts.snapshot import FeatureSnapshot
from trading.domain.enums import FamilyId, InstrumentKind, Side
from trading.domain.primitives import Lots, LotSize, Money, Percent, Price, Rounding
from trading.risk.limits import (
    floor_divide_money,
    open_trade_slots,
    premium_budget_used,
    underlying_exposure_for,
)
from trading.risk.sizing.long_option import LotBounds

__all__ = [
    "ButterflyLegs",
    "LongButterflySizingEngine",
    "LongButterflySizingResult",
    "butterfly_legs",
    "is_long_butterfly",
]

_BUTTERFLY_LEG_COUNT = 3
_MIDDLE_RATIO = 2


@dataclass(frozen=True, slots=True)
class ButterflyLegs:
    """Validated 1:2:1 long butterfly legs."""

    low_wing: IntentLeg
    short_body: IntentLeg
    high_wing: IntentLeg


@dataclass(frozen=True, slots=True)
class LongButterflySizingResult:
    """Sized outcome for a long butterfly."""

    bounds: LotBounds
    approved_lots: int
    cost_per_lot: Money
    estimated_margin: Money
    recalculated_max_loss: Money
    net_debit_per_unit: Price
    lot_size: LotSize
    legs: ButterflyLegs


def is_long_butterfly(intent: TradeIntent) -> bool:
    """Return True when the intent expresses a 1:2:1 long butterfly."""
    if intent.family_id not in {
        FamilyId.long_call_butterfly.value,
        FamilyId.long_put_butterfly.value,
    }:
        return False
    try:
        butterfly_legs(intent)
    except ValueError:
        return False
    return True


def butterfly_legs(intent: TradeIntent) -> ButterflyLegs:
    """Return validated long butterfly legs ordered low wing, body, high wing."""
    if len(intent.legs) != _BUTTERFLY_LEG_COUNT:
        raise ValueError("long butterfly requires exactly three legs")
    option_types = {leg.contract.option_type for leg in intent.legs}
    if len(option_types) != 1:
        raise ValueError("long butterfly legs must share one option type")
    buys = [leg for leg in intent.legs if leg.side is Side.BUY]
    sells = [leg for leg in intent.legs if leg.side is Side.SELL]
    if len(buys) != 2 or len(sells) != 1:
        raise ValueError("long butterfly requires two buys and one sell")
    if sells[0].ratio != _MIDDLE_RATIO:
        raise ValueError("long butterfly body must carry ratio 2")
    low_wing = min(buys, key=lambda leg: leg.contract.strike or Decimal("0"))
    high_wing = max(buys, key=lambda leg: leg.contract.strike or Decimal("0"))
    short_body = sells[0]
    strikes = [leg.contract.strike for leg in intent.legs]
    if any(strike is None for strike in strikes):
        raise ValueError("long butterfly legs require strikes")
    low_strike = low_wing.contract.strike
    mid_strike = short_body.contract.strike
    high_strike = high_wing.contract.strike
    if low_strike is None or mid_strike is None or high_strike is None:
        raise ValueError("long butterfly legs require strikes")
    if not (low_strike < mid_strike < high_strike):
        raise ValueError("long butterfly strikes must satisfy low < mid < high")
    if high_strike - mid_strike != mid_strike - low_strike:
        raise ValueError("long butterfly wings must be symmetric")
    expiries = {leg.contract.expiry for leg in intent.legs}
    if len(expiries) != 1:
        raise ValueError("long butterfly legs must share one expiry")
    if not all(
        leg.contract.instrument_kind is InstrumentKind.OPTION for leg in intent.legs
    ):
        raise ValueError("long butterfly legs must be options")
    return ButterflyLegs(low_wing=low_wing, short_body=short_body, high_wing=high_wing)


class LongButterflySizingEngine:
    """Size long butterflies from net debit."""

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
    ) -> LongButterflySizingResult:
        """Compute lots from net debit and the min() formula."""
        legs = butterfly_legs(request.intent)
        low = _leg_snapshot(leg_snapshots, legs.low_wing.leg_id)
        body = _leg_snapshot(leg_snapshots, legs.short_body.leg_id)
        high = _leg_snapshot(leg_snapshots, legs.high_wing.leg_id)
        net_debit_per_unit = _net_debit_per_unit(low, body, high)
        currency = request.limits.max_loss_per_trade.currency
        one_lot = Lots(1).to_quantity(lot_size)
        loss_per_lot = net_debit_per_unit.notional(one_lot, currency)
        slippage = Percent.from_fraction(policy.slippage_buffer_fraction).of(
            loss_per_lot
        )
        charges = (policy.charges_per_lot.to_money() * 2).quantized(Rounding.CEILING)
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
            legs=legs,
            lot_size=lot_size,
            margin_available=request.limits.margin_available,
        )
        portfolio_limit_lots = self._portfolio_limit_lots(
            request,
            policy,
            cost_per_lot,
            loss_per_lot,
            account_risk=account_risk,
            snapshots=(low, body, high),
            lot_size=lot_size,
        )
        liquidity_lots = self._liquidity_lots(low, body, high, lot_size)

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

        return LongButterflySizingResult(
            bounds=bounds,
            approved_lots=approved_lots,
            cost_per_lot=cost_per_lot,
            estimated_margin=estimated_margin,
            recalculated_max_loss=recalculated_max_loss,
            net_debit_per_unit=net_debit_per_unit,
            lot_size=lot_size,
            legs=legs,
        )

    @staticmethod
    def _margin_lots(
        margin_preview: MarginPreviewPort,
        *,
        account_id: str,
        preview_request_id: str,
        legs: ButterflyLegs,
        lot_size: LotSize,
        margin_available: Money,
    ) -> tuple[int, Money]:
        quantity = Lots(1).to_quantity(lot_size)
        body_qty = quantity.contracts * legs.short_body.ratio
        preview = margin_preview.preview_margin(
            MarginPreviewRequest(
                request_id=preview_request_id,
                account_id=account_id,
                legs=(
                    MarginPreviewLeg(
                        contract=legs.low_wing.contract,
                        side=Side.BUY,
                        quantity_contracts=quantity.contracts,
                    ),
                    MarginPreviewLeg(
                        contract=legs.short_body.contract,
                        side=Side.SELL,
                        quantity_contracts=body_qty,
                    ),
                    MarginPreviewLeg(
                        contract=legs.high_wing.contract,
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
        snapshots: tuple[FeatureSnapshot, FeatureSnapshot, FeatureSnapshot],
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
        current_notional = (
            current.gross_notional
            if current is not None
            else Money.zero(equity.currency)
        )
        concentration_remaining = concentration_cap - current_notional
        concentration_lots = floor_divide_money(concentration_remaining, loss_per_lot)
        return min(premium_lots, concentration_lots, slots)

    @staticmethod
    def _liquidity_lots(
        low: FeatureSnapshot,
        body: FeatureSnapshot,
        high: FeatureSnapshot,
        lot_size: LotSize,
    ) -> int:
        depths = [_leg_depth(snapshot) for snapshot in (low, body, high)]
        resolved = [depth for depth in depths if depth is not None]
        if len(resolved) != len(depths):
            return 10_000
        body_depth = resolved[1] // _MIDDLE_RATIO
        depth = min(resolved[0], body_depth, resolved[2])
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
    low: FeatureSnapshot,
    body: FeatureSnapshot,
    high: FeatureSnapshot,
) -> Price:
    low_ask = low.market.ask
    body_bid = body.market.bid
    high_ask = high.market.ask
    if low_ask is None or body_bid is None or high_ask is None:
        raise ValueError("butterfly sizing requires wing asks and body bid")
    net_value = low_ask.value + high_ask.value - (Decimal("2") * body_bid.value)
    if net_value <= 0:
        raise ValueError("long butterfly net debit must be positive")
    return Price(net_value, low_ask.tick)


def _leg_depth(feature: FeatureSnapshot) -> int | None:
    bid_size = feature.market.bid_size
    ask_size = feature.market.ask_size
    if bid_size is None or ask_size is None:
        return None
    return min(bid_size, ask_size)
