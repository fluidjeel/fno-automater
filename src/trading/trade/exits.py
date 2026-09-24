"""Deterministic exit evaluation for open positions."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum, unique

from trading.domain.contracts.intent import ExitTemplate, IntentLeg, TradeIntent
from trading.domain.contracts.position import (
    ExitPolicy,
    PositionLegState,
    PositionState,
)
from trading.domain.contracts.snapshot import FeatureSnapshot
from trading.domain.contracts.terminal_policy import TerminalPolicy
from trading.domain.enums import ExitScope, ReasonCode, Side, TradeState
from trading.domain.primitives import Currency, Money, Price, Rounding
from trading.risk.sizing.credit_spread import is_credit_spread

__all__ = [
    "ExitEngine",
    "ExitEvaluation",
    "ExitKind",
    "attach_terminal_policy",
    "build_exit_policy",
    "monitor_leg",
    "strategy_unrealized_pnl",
]


@unique
class ExitKind(StrEnum):
    """Why an exit evaluation fired."""

    NONE = "NONE"
    STOP = "STOP"
    TARGET = "TARGET"
    TIME = "TIME"


@dataclass(frozen=True, slots=True)
class ExitEvaluation:
    """Outcome of one deterministic exit evaluation pass."""

    kind: ExitKind
    reason_code: ReasonCode
    detail: str
    updated_policy: ExitPolicy | None = None

    @property
    def should_exit(self) -> bool:
        return self.kind is not ExitKind.NONE


class ExitEngine:
    """Evaluate stop, target, time and monotonic trail rules."""

    def evaluate(
        self,
        position: PositionState,
        feature: FeatureSnapshot,
        intent: TradeIntent,
        *,
        now: datetime,
        leg_snapshots: Mapping[str, FeatureSnapshot] | None = None,
    ) -> ExitEvaluation:
        """Return an exit signal and any tightened stop policy."""
        if position.state is not TradeState.OPEN:
            return _no_exit(position.exit_policy)
        scope = position.exit_policy.scope
        if scope in {ExitScope.STRATEGY_PNL, ExitScope.SPREAD_VALUE}:
            return self._evaluate_strategy_pnl(
                position,
                intent,
                feature=feature,
                leg_snapshots=leg_snapshots,
                now=now,
            )
        if scope is not ExitScope.LEG_PRICE:
            raise ValueError(f"unsupported exit scope {scope}")
        leg = _price_exit_leg(position, intent)
        monitor = None if leg is None else _monitor_price(feature, leg.side)
        if leg is None or monitor is None:
            return ExitEvaluation(
                kind=ExitKind.NONE,
                reason_code=ReasonCode.PRICE_UNAVAILABLE,
                detail=(
                    "monitor leg is not present on the position"
                    if leg is None
                    else "exit monitor price unavailable"
                ),
            )

        tightened = tighten_exit_policy(
            position.exit_policy,
            intent.exit_template,
            entry_price=leg.average_entry_price,
            monitor_price=monitor,
        )
        policy = tightened or position.exit_policy
        exit_kind = ExitKind.NONE
        detail = "no exit condition met"
        if policy.time_exit is not None and now >= policy.time_exit:
            exit_kind = ExitKind.TIME
            detail = "scheduled time exit reached"
        elif policy.stop_price is not None and _stop_hit(
            leg.side, monitor, policy.stop_price
        ):
            exit_kind = ExitKind.STOP
            detail = "stop price breached"
        elif policy.target_price is not None and _target_hit(
            leg.side, monitor, policy.target_price
        ):
            exit_kind = ExitKind.TARGET
            detail = "target price reached"

        if exit_kind is not ExitKind.NONE:
            return ExitEvaluation(
                kind=exit_kind,
                reason_code=ReasonCode.OK,
                detail=detail,
                updated_policy=policy,
            )
        if tightened is not None:
            return ExitEvaluation(
                kind=ExitKind.NONE,
                reason_code=ReasonCode.OK,
                detail="stop tightened",
                updated_policy=tightened,
            )
        return _no_exit(position.exit_policy)

    def _evaluate_missing_snapshots(
        self,
        position: PositionState,
        intent: TradeIntent,
        feature: FeatureSnapshot | None,
    ) -> ExitEvaluation:
        policy = position.exit_policy
        if policy.stop_price is not None and feature is not None:
            leg = _price_exit_leg(position, intent)
            monitor = None if leg is None else _monitor_price(feature, leg.side)
            if (
                leg is not None
                and monitor is not None
                and _stop_hit(leg.side, monitor, policy.stop_price)
            ):
                return ExitEvaluation(
                    kind=ExitKind.STOP,
                    reason_code=ReasonCode.OK,
                    detail="auxiliary leg stop price breached",
                    updated_policy=policy,
                )
        return ExitEvaluation(
            kind=ExitKind.NONE,
            reason_code=ReasonCode.PRICE_UNAVAILABLE,
            detail="strategy P&L exit requires leg snapshots",
        )

    @staticmethod
    def _determine_strategy_exit(
        position: PositionState,
        intent: TradeIntent,
        pnl: Money,
        leg_snapshots: Mapping[str, FeatureSnapshot],
        now: datetime,
    ) -> tuple[ExitKind, str]:
        policy = position.exit_policy
        if policy.time_exit is not None and now >= policy.time_exit:
            return ExitKind.TIME, "scheduled time exit reached"
        if policy.pnl_stop is not None and pnl <= policy.pnl_stop:
            return ExitKind.STOP, "strategy P&L stop breached"
        if policy.pnl_target is not None and pnl >= policy.pnl_target:
            return ExitKind.TARGET, "strategy P&L target reached"
        if ExitEngine._auxiliary_stop_breached(position, intent, policy, leg_snapshots):
            return ExitKind.STOP, "auxiliary leg stop price breached"
        return ExitKind.NONE, "no exit condition met"

    def _evaluate_strategy_pnl(
        self,
        position: PositionState,
        intent: TradeIntent,
        *,
        feature: FeatureSnapshot | None = None,
        leg_snapshots: Mapping[str, FeatureSnapshot] | None,
        now: datetime,
    ) -> ExitEvaluation:
        policy = position.exit_policy
        if leg_snapshots is None:
            return self._evaluate_missing_snapshots(position, intent, feature)
        pnl = strategy_unrealized_pnl(position, intent, leg_snapshots)
        if pnl is None:
            return ExitEvaluation(
                kind=ExitKind.NONE,
                reason_code=ReasonCode.PRICE_UNAVAILABLE,
                detail="strategy P&L mark unavailable",
            )
        kind, detail = self._determine_strategy_exit(
            position, intent, pnl, leg_snapshots, now
        )
        if kind is not ExitKind.NONE:
            return ExitEvaluation(
                kind=kind,
                reason_code=ReasonCode.OK,
                detail=detail,
                updated_policy=policy,
            )
        return _no_exit(policy)

    @staticmethod
    def _auxiliary_stop_breached(
        position: PositionState,
        intent: TradeIntent,
        policy: ExitPolicy,
        leg_snapshots: Mapping[str, FeatureSnapshot],
    ) -> bool:
        if policy.stop_price is None:
            return False
        leg = _price_exit_leg(position, intent)
        if leg is None:
            return False
        intent_leg = next(
            (item for item in intent.legs if item.leg_id == leg.leg_id), None
        )
        if intent_leg is None:
            return False
        leg_snap = _snapshot_for_leg(intent_leg, leg_snapshots)
        if leg_snap is None:
            return False
        monitor = _monitor_price(leg_snap, leg.side)
        return monitor is not None and _stop_hit(leg.side, monitor, policy.stop_price)


def build_exit_policy(
    template: ExitTemplate,
    *,
    trade_id: str,
    policy_id: str,
    entry_price: Price,
    initialized_at: datetime,
    scope: ExitScope = ExitScope.LEG_PRICE,
    quantity_contracts: int = 1,
    entry_strategy_pnl: Money | None = None,
    monitor_side: Side = Side.BUY,
    terminal_policy: TerminalPolicy | None = None,
) -> ExitPolicy:
    """Initialize runtime exit policy from an intent template at entry."""
    tick = entry_price.tick
    distance = Decimal(template.stop_distance_ticks) * tick.value
    stop_price: Price | None
    if monitor_side is Side.BUY:
        stop_value = entry_price.value - distance
        stop_price = Price.snap(stop_value, tick, rounding=Rounding.FLOOR)
    else:
        stop_value = entry_price.value + distance
        stop_price = Price.snap(stop_value, tick, rounding=Rounding.CEILING)
    target_price: Price | None = None
    if template.target_distance_ticks is not None:
        if monitor_side is Side.BUY:
            target_value = (
                entry_price.value + Decimal(template.target_distance_ticks) * tick.value
            )
        else:
            target_value = (
                entry_price.value - Decimal(template.target_distance_ticks) * tick.value
            )
        target_price = Price.snap(target_value, tick)
    pnl_stop: Money | None = None
    pnl_target: Money | None = None
    if scope in {ExitScope.STRATEGY_PNL, ExitScope.SPREAD_VALUE}:
        currency = (
            entry_strategy_pnl.currency
            if entry_strategy_pnl is not None
            else Currency.INR
        )
        per_contract = Decimal(template.stop_distance_ticks) * tick.value
        stop_amount = (per_contract * quantity_contracts).quantize(Decimal("0.01"))
        pnl_stop = Money(-stop_amount, currency)
        if template.target_distance_ticks is not None:
            target_amount = (
                Decimal(template.target_distance_ticks)
                * tick.value
                * quantity_contracts
            ).quantize(Decimal("0.01"))
            pnl_target = Money(target_amount, currency)
    return ExitPolicy(
        policy_id=policy_id,
        trade_id=trade_id,
        scope=scope,
        initial_stop_distance_ticks=template.stop_distance_ticks,
        current_stop_distance_ticks=template.stop_distance_ticks,
        stop_price=stop_price,
        target_price=target_price,
        strategy_entry_pnl=entry_strategy_pnl,
        pnl_stop=pnl_stop,
        pnl_target=pnl_target,
        time_exit=template.time_exit,
        exit_before_expiry_days=template.exit_before_expiry_days,
        terminal_policy=terminal_policy,
        initialized_at=initialized_at,
    )


def attach_terminal_policy(
    policy: ExitPolicy, terminal_policy: TerminalPolicy
) -> ExitPolicy:
    """Freeze an accepted TerminalPolicy onto ExitPolicy at entry (ADESK-C2).

    Does not alter stops. Replacing an existing terminal_policy is refused so
    run-to-expiry cannot be re-granted after a later revert (C3 one-way door
    starts here at the type boundary).
    """
    if policy.trade_id != terminal_policy.trade_id:
        raise ValueError(
            f"terminal_policy trade_id {terminal_policy.trade_id} != "
            f"exit policy {policy.trade_id}"
        )
    if policy.terminal_policy is not None:
        raise ValueError(
            "ExitPolicy already carries a frozen terminal_policy; refuse replace"
        )
    return policy.model_copy(update={"terminal_policy": terminal_policy})


def monitor_leg(intent: TradeIntent) -> IntentLeg:
    """Return the leg whose price drives LEG_PRICE exit monitoring."""
    if is_credit_spread(intent):
        return next(leg for leg in intent.legs if leg.side is Side.SELL)
    buy_legs = [leg for leg in intent.legs if leg.side is Side.BUY]
    if buy_legs:
        return buy_legs[0]
    return intent.legs[0]


def _price_exit_leg(
    position: PositionState, intent: TradeIntent
) -> PositionLegState | None:
    watched = monitor_leg(intent)
    for leg in position.legs:
        if leg.leg_id == watched.leg_id:
            return leg
    return None


def _snapshot_for_leg(
    intent_leg: IntentLeg,
    leg_snapshots: Mapping[str, FeatureSnapshot],
) -> FeatureSnapshot | None:
    """Find a feature snapshot for a leg by leg_id, symbol, or contract symbol."""
    if intent_leg.leg_id in leg_snapshots:
        return leg_snapshots[intent_leg.leg_id]
    symbol = intent_leg.contract.symbol
    if symbol in leg_snapshots:
        return leg_snapshots[symbol]
    for snap in leg_snapshots.values():
        if snap.contract.symbol == symbol:
            return snap
    return None


def strategy_unrealized_pnl(
    position: PositionState,
    intent: TradeIntent,
    leg_snapshots: Mapping[str, FeatureSnapshot],
) -> Money | None:
    """Mark-to-market strategy P&L from entry fills and current leg quotes."""
    if not position.legs:
        return None
    leg_by_id = {leg.leg_id: leg for leg in position.legs}
    total = Decimal(0)
    for intent_leg in intent.legs:
        position_leg = leg_by_id.get(intent_leg.leg_id)
        snapshot = _snapshot_for_leg(intent_leg, leg_snapshots)
        if position_leg is None or snapshot is None:
            return None
        leg_pnl = _leg_unrealized_pnl(
            intent_leg.side,
            position_leg.average_entry_price,
            snapshot,
            Decimal(position_leg.quantity_contracts),
        )
        if leg_pnl is None:
            return None
        total += leg_pnl
    return Money(total.quantize(Decimal("0.01")), Currency.INR)


def _leg_unrealized_pnl(
    side: Side,
    entry: Price,
    feature: FeatureSnapshot,
    quantity: Decimal,
) -> Decimal | None:
    quote = feature.market
    if side is Side.BUY:
        bid = quote.bid
        if bid is None:
            return None
        return (bid.value - entry.value) * quantity
    ask = quote.ask
    if ask is None:
        return None
    return (entry.value - ask.value) * quantity


def tighten_exit_policy(
    policy: ExitPolicy,
    template: ExitTemplate,
    *,
    entry_price: Price,
    monitor_price: Price,
) -> ExitPolicy | None:
    """Tighten stops monotonically. Invariant 17."""
    tick = entry_price.tick
    updated = policy
    changed = False

    if template.break_even_trigger_ticks is not None and not policy.breakeven_active:
        be_trigger = template.break_even_trigger_ticks
        trigger = entry_price.value + Decimal(be_trigger) * tick.value
        if monitor_price.value >= trigger:
            breakeven_stop = entry_price
            stop = policy.stop_price
            if stop is None or breakeven_stop.value > stop.value:
                distance = int((entry_price.value - breakeven_stop.value) / tick.value)
                updated = updated.model_copy(
                    update={
                        "breakeven_active": True,
                        "stop_price": breakeven_stop,
                        "current_stop_distance_ticks": min(
                            updated.current_stop_distance_ticks,
                            max(distance, 1),
                        ),
                    }
                )
                changed = True

    activation = template.trailing_activation_ticks
    trail_distance = template.trailing_distance_ticks
    if activation is not None and trail_distance is not None:
        activation_price = entry_price.value + Decimal(activation) * tick.value
        if monitor_price.value >= activation_price:
            trail_value = monitor_price.value - Decimal(trail_distance) * tick.value
            trail_stop = Price.snap(trail_value, tick, rounding=Rounding.FLOOR)
            if policy.stop_price is None or trail_stop.value > policy.stop_price.value:
                stop_distance = int((entry_price.value - trail_stop.value) / tick.value)
                updated = updated.model_copy(
                    update={
                        "trailing_active": True,
                        "stop_price": trail_stop,
                        "current_stop_distance_ticks": min(
                            updated.current_stop_distance_ticks,
                            max(stop_distance, 1),
                        ),
                    }
                )
                changed = True

    return updated if changed else None


def _monitor_price(feature: FeatureSnapshot, side: Side) -> Price | None:
    quote = feature.market
    if side is Side.BUY:
        return quote.bid
    return quote.ask


def _stop_hit(side: Side, monitor: Price, stop: Price) -> bool:
    if side is Side.BUY:
        return monitor.value <= stop.value
    return monitor.value >= stop.value


def _target_hit(side: Side, monitor: Price, target: Price) -> bool:
    if side is Side.BUY:
        return monitor.value >= target.value
    return monitor.value <= target.value


def _no_exit(policy: ExitPolicy) -> ExitEvaluation:
    return ExitEvaluation(
        kind=ExitKind.NONE,
        reason_code=ReasonCode.OK,
        detail="no exit condition met",
        updated_policy=None,
    )
