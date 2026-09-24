"""Long straddle and strangle sizing: total debit with bounded maximum loss."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

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
from trading.domain.enums import FamilyId, InstrumentKind, OptionType, Side
from trading.domain.primitives import Lots, LotSize, Money, Percent, Price, Rounding
from trading.risk.limits import (
    floor_divide_money,
    open_trade_slots,
    premium_budget_used,
    underlying_exposure_for,
)
from trading.risk.sizing.long_option import LotBounds

__all__ = [
    "LongStraddleSizingEngine",
    "LongStraddleSizingResult",
    "LongStrangleSizingEngine",
    "LongStrangleSizingResult",
    "is_long_straddle",
    "is_long_strangle",
    "straddle_legs",
    "strangle_legs",
]

_VOLATILITY_LEG_COUNT = 2


@dataclass(frozen=True, slots=True)
class LongStraddleSizingResult:
    """Sized outcome for a long straddle."""

    bounds: LotBounds
    approved_lots: int
    cost_per_lot: Money
    estimated_margin: Money
    recalculated_max_loss: Money
    net_debit_per_unit: Price
    lot_size: LotSize
    call_leg: IntentLeg
    put_leg: IntentLeg


@dataclass(frozen=True, slots=True)
class LongStrangleSizingResult:
    """Sized outcome for a long strangle."""

    bounds: LotBounds
    approved_lots: int
    cost_per_lot: Money
    estimated_margin: Money
    recalculated_max_loss: Money
    net_debit_per_unit: Price
    lot_size: LotSize
    call_leg: IntentLeg
    put_leg: IntentLeg


def is_long_straddle(intent: TradeIntent) -> bool:
    """Return True when the intent expresses a long straddle."""
    if intent.family_id != FamilyId.long_straddle.value:
        return False
    try:
        straddle_legs(intent)
    except ValueError:
        return False
    return True


def is_long_strangle(intent: TradeIntent) -> bool:
    """Return True when the intent expresses a long strangle."""
    if intent.family_id != FamilyId.long_strangle.value:
        return False
    try:
        strangle_legs(intent)
    except ValueError:
        return False
    return True


def straddle_legs(intent: TradeIntent) -> tuple[IntentLeg, IntentLeg]:
    """Return call and put legs for a validated long straddle."""
    if len(intent.legs) != _VOLATILITY_LEG_COUNT:
        raise ValueError("long straddle requires exactly two legs")
    call = next(
        (leg for leg in intent.legs if leg.contract.option_type is OptionType.CALL),
        None,
    )
    put = next(
        (leg for leg in intent.legs if leg.contract.option_type is OptionType.PUT),
        None,
    )
    if call is None or put is None:
        raise ValueError("long straddle requires one call and one put")
    if call.side is not Side.BUY or put.side is not Side.BUY:
        raise ValueError("long straddle requires two BUY legs")
    call_strike = call.contract.strike
    put_strike = put.contract.strike
    if call_strike is None or put_strike is None:
        raise ValueError("long straddle legs require strikes")
    if call_strike != put_strike:
        raise ValueError("long straddle legs must share the same strike")
    if call.contract.expiry != put.contract.expiry:
        raise ValueError("long straddle legs must share one expiry")
    if not all(
        leg.contract.instrument_kind is InstrumentKind.OPTION for leg in intent.legs
    ):
        raise ValueError("long straddle legs must be options")
    return call, put


def strangle_legs(intent: TradeIntent) -> tuple[IntentLeg, IntentLeg]:
    """Return call and put legs for a validated long strangle."""
    if len(intent.legs) != _VOLATILITY_LEG_COUNT:
        raise ValueError("long strangle requires exactly two legs")
    call = next(
        (leg for leg in intent.legs if leg.contract.option_type is OptionType.CALL),
        None,
    )
    put = next(
        (leg for leg in intent.legs if leg.contract.option_type is OptionType.PUT),
        None,
    )
    if call is None or put is None:
        raise ValueError("long strangle requires one call and one put")
    if call.side is not Side.BUY or put.side is not Side.BUY:
        raise ValueError("long strangle requires two BUY legs")
    call_strike = call.contract.strike
    put_strike = put.contract.strike
    if call_strike is None or put_strike is None:
        raise ValueError("long strangle legs require strikes")
    if put_strike >= call_strike:
        raise ValueError("long strangle put strike must be below call strike")
    if call.contract.expiry != put.contract.expiry:
        raise ValueError("long strangle legs must share one expiry")
    if not all(
        leg.contract.instrument_kind is InstrumentKind.OPTION for leg in intent.legs
    ):
        raise ValueError("long strangle legs must be options")
    return call, put


class LongStraddleSizingEngine:
    """Size long straddles from total debit."""

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
    ) -> LongStraddleSizingResult:
        call, put = straddle_legs(request.intent)
        return cast(
            LongStraddleSizingResult,
            _size_volatility_debit(
                request,
                leg_snapshots,
                policy,
                margin_preview,
                account_id=account_id,
                account_risk=account_risk,
                preview_request_id=preview_request_id,
                lot_size=lot_size,
                call=call,
                put=put,
                result_cls=LongStraddleSizingResult,
            ),
        )


class LongStrangleSizingEngine:
    """Size long strangles from total debit."""

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
    ) -> LongStrangleSizingResult:
        call, put = strangle_legs(request.intent)
        return cast(
            LongStrangleSizingResult,
            _size_volatility_debit(
                request,
                leg_snapshots,
                policy,
                margin_preview,
                account_id=account_id,
                account_risk=account_risk,
                preview_request_id=preview_request_id,
                lot_size=lot_size,
                call=call,
                put=put,
                result_cls=LongStrangleSizingResult,
            ),
        )


def _size_volatility_debit(
    request: SizingRequest,
    leg_snapshots: Mapping[str, FeatureSnapshot],
    policy: RiskPolicyConfig,
    margin_preview: MarginPreviewPort,
    *,
    account_id: str,
    account_risk: RiskLimits,
    preview_request_id: str,
    lot_size: LotSize,
    call: IntentLeg,
    put: IntentLeg,
    result_cls: type[LongStraddleSizingResult | LongStrangleSizingResult],
) -> LongStraddleSizingResult | LongStrangleSizingResult:
    call_snapshot = _leg_snapshot(leg_snapshots, call.leg_id)
    put_snapshot = _leg_snapshot(leg_snapshots, put.leg_id)
    net_debit_per_unit = _net_debit_per_unit(call_snapshot, put_snapshot)
    currency = request.limits.max_loss_per_trade.currency
    one_lot = Lots(1).to_quantity(lot_size)
    loss_per_lot = net_debit_per_unit.notional(one_lot, currency)
    slippage = Percent.from_fraction(policy.slippage_buffer_fraction).of(loss_per_lot)
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
    margin_lots, margin_per_lot = _margin_lots(
        margin_preview,
        account_id=account_id,
        preview_request_id=preview_request_id,
        call=call,
        put=put,
        lot_size=lot_size,
        margin_available=request.limits.margin_available,
    )
    portfolio_limit_lots = _portfolio_limit_lots(
        request,
        policy,
        cost_per_lot,
        loss_per_lot,
        account_risk=account_risk,
        snapshots=(call_snapshot, put_snapshot),
        lot_size=lot_size,
    )
    liquidity_lots = _liquidity_lots(call_snapshot, put_snapshot, lot_size)

    bounds = LotBounds(
        risk_lots=risk_lots,
        capital_lots=capital_lots,
        margin_lots=margin_lots,
        portfolio_limit_lots=portfolio_limit_lots,
        liquidity_lots=liquidity_lots,
    )
    approved_lots = bounds.approved_lots
    recalculated_max_loss = (cost_per_lot * approved_lots).quantized(Rounding.CEILING)
    if approved_lots > 0:
        estimated_margin = (margin_per_lot * approved_lots).quantized(Rounding.CEILING)
    else:
        estimated_margin = Money.zero(currency)

    return result_cls(
        bounds=bounds,
        approved_lots=approved_lots,
        cost_per_lot=cost_per_lot,
        estimated_margin=estimated_margin,
        recalculated_max_loss=recalculated_max_loss,
        net_debit_per_unit=net_debit_per_unit,
        lot_size=lot_size,
        call_leg=call,
        put_leg=put,
    )


def _leg_snapshot(
    leg_snapshots: Mapping[str, FeatureSnapshot],
    leg_id: str,
) -> FeatureSnapshot:
    snapshot = leg_snapshots.get(leg_id)
    if snapshot is None:
        raise ValueError(f"missing feature snapshot for leg {leg_id}")
    return snapshot


def _net_debit_per_unit(call: FeatureSnapshot, put: FeatureSnapshot) -> Price:
    call_ask = call.market.ask
    put_ask = put.market.ask
    if call_ask is None or put_ask is None:
        raise ValueError("volatility debit sizing requires both asks")
    net_value = call_ask.value + put_ask.value
    if net_value <= 0:
        raise ValueError("volatility debit must be positive")
    return Price(net_value, call_ask.tick)


def _margin_lots(
    margin_preview: MarginPreviewPort,
    *,
    account_id: str,
    preview_request_id: str,
    call: IntentLeg,
    put: IntentLeg,
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
                    contract=call.contract,
                    side=Side.BUY,
                    quantity_contracts=quantity.contracts,
                ),
                MarginPreviewLeg(
                    contract=put.contract,
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
    request: SizingRequest,
    policy: RiskPolicyConfig,
    cost_per_lot: Money,
    loss_per_lot: Money,
    *,
    account_risk: RiskLimits,
    snapshots: tuple[FeatureSnapshot, FeatureSnapshot],
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
    concentration_cap = (equity * policy.underlying_concentration_fraction).quantized(
        Rounding.FLOOR
    )
    current = underlying_exposure_for(portfolio, request.intent.underlying)
    current_notional = (
        current.gross_notional if current is not None else Money.zero(equity.currency)
    )
    concentration_remaining = concentration_cap - current_notional
    concentration_lots = floor_divide_money(concentration_remaining, loss_per_lot)
    return min(premium_lots, concentration_lots, slots)


def _liquidity_lots(
    call: FeatureSnapshot,
    put: FeatureSnapshot,
    lot_size: LotSize,
) -> int:
    depths = [_leg_depth(snapshot) for snapshot in (call, put)]
    resolved = [depth for depth in depths if depth is not None]
    if len(resolved) != len(depths):
        return 10_000
    depth = min(resolved)
    return max(depth // lot_size.contracts_per_lot, 0)


def _leg_depth(feature: FeatureSnapshot) -> int | None:
    bid_size = feature.market.bid_size
    ask_size = feature.market.ask_size
    if bid_size is None or ask_size is None:
        return None
    return min(bid_size, ask_size)
