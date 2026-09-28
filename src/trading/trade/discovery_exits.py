"""DISCOVERY-only per-leg and structure exit policy construction."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from trading.config.discovery import DiscoveryExitsConfig
from trading.domain.contracts.intent import ExitTemplate, IntentLeg, TradeIntent
from trading.domain.contracts.position import (
    ExitPolicy,
    PositionLegState,
    PositionState,
)
from trading.domain.contracts.snapshot import FeatureSnapshot
from trading.domain.enums import ExitScope, Side
from trading.domain.primitives import Currency, Money, Price, Rounding

__all__ = [
    "DiscoveryExitEvaluationState",
    "apply_discovery_leg_exit_prices",
    "apply_discovery_trailing",
    "build_discovery_exit_policy",
    "compute_leg_stop_price",
    "compute_leg_target_price",
    "evaluate_discovery_confirmation",
    "is_premium_scaled_policy",
    "leg_stop_target_breached_mid",
    "structure_net_debit_per_unit",
    "structure_pnl_bounds",
    "structure_unrealized_pnl_mid",
]


@dataclass(frozen=True, slots=True)
class DiscoveryExitEvaluationState:
    """Mutable discovery exit state carried on ExitPolicy between evaluations."""

    policy: ExitPolicy
    exit_kind: str
    detail: str
    should_exit: bool


def is_premium_scaled_policy(policy: ExitPolicy) -> bool:
    """Return True when the policy uses DISCOVERY premium-scaled exits."""
    return policy.premium_scaled


def compute_leg_stop_price(
    *,
    entry_price: Price,
    side: Side,
    config: DiscoveryExitsConfig,
) -> Price:
    """Per-leg stop from this leg's entry; never copied from another leg."""
    tick = entry_price.tick
    if side is Side.BUY:
        stop_value = entry_price.value * (Decimal(1) - config.leg_stop_fraction)
        return Price.snap(stop_value, tick, rounding=Rounding.FLOOR)
    stop_value = entry_price.value * config.short_leg_stop_multiple
    return Price.snap(stop_value, tick, rounding=Rounding.CEILING)


def compute_leg_target_price(
    *,
    entry_price: Price,
    side: Side,
    config: DiscoveryExitsConfig,
) -> Price:
    """Per-leg target from this leg's entry."""
    tick = entry_price.tick
    if side is Side.BUY:
        target_value = entry_price.value * (Decimal(1) + config.leg_target_fraction)
        return Price.snap(target_value, tick, rounding=Rounding.CEILING)
    target_value = entry_price.value * config.short_leg_target_fraction
    return Price.snap(target_value, tick, rounding=Rounding.FLOOR)


def structure_pnl_bounds(
    legs: tuple[PositionLegState, ...],
    config: DiscoveryExitsConfig,
    *,
    max_loss: Money | None = None,
) -> tuple[Money, Money]:
    """Structure pnl_stop/pnl_target consistent with per-leg fractions."""
    stop_sum = Decimal(0)
    target_sum = Decimal(0)
    currency = Currency.INR
    for leg in legs:
        qty = Decimal(leg.quantity_contracts)
        entry = leg.average_entry_price.value
        if leg.side is Side.BUY:
            stop_sum += config.leg_stop_fraction * entry * qty
            target_sum += config.leg_target_fraction * entry * qty
        else:
            stop_sum += (config.short_leg_stop_multiple - Decimal(1)) * entry * qty
            target_sum += (Decimal(1) - config.short_leg_target_fraction) * entry * qty
    if max_loss is not None:
        stop_sum = min(stop_sum, max_loss.amount)
    return (
        Money((-stop_sum).quantize(Decimal("0.01")), currency),
        Money(target_sum.quantize(Decimal("0.01")), currency),
    )


def apply_discovery_leg_exit_prices(
    legs: tuple[PositionLegState, ...],
    config: DiscoveryExitsConfig,
) -> tuple[PositionLegState, ...]:
    """Stamp each leg with stop/target derived from its own average entry."""
    updated: list[PositionLegState] = []
    for leg in legs:
        stop = compute_leg_stop_price(
            entry_price=leg.average_entry_price,
            side=leg.side,
            config=config,
        )
        target = compute_leg_target_price(
            entry_price=leg.average_entry_price,
            side=leg.side,
            config=config,
        )
        updated.append(
            leg.model_copy(
                update={
                    "current_stop_price": stop,
                    "current_target_price": target,
                }
            )
        )
    return tuple(updated)


def build_discovery_exit_policy(
    intent: TradeIntent,
    legs: tuple[PositionLegState, ...],
    template: ExitTemplate,
    *,
    trade_id: str,
    policy_id: str,
    initialized_at: datetime,
    config: DiscoveryExitsConfig,
    scope: ExitScope,
    max_loss: Money | None = None,
) -> ExitPolicy:
    """Build a frozen premium-scaled exit policy for DISCOVERY PAPER entries."""
    if not legs:
        raise ValueError("discovery exit policy requires at least one leg")
    pnl_stop, pnl_target = structure_pnl_bounds(legs, config, max_loss=max_loss)
    return ExitPolicy(
        policy_id=policy_id,
        trade_id=trade_id,
        scope=scope,
        initial_stop_distance_ticks=1,
        current_stop_distance_ticks=1,
        stop_price=None,
        target_price=None,
        strategy_entry_pnl=Money(Decimal(0), Currency.INR),
        pnl_stop=pnl_stop,
        pnl_target=pnl_target,
        time_exit=template.time_exit,
        exit_before_expiry_days=template.exit_before_expiry_days,
        initialized_at=initialized_at,
        premium_scaled=True,
        exit_confirm_required=config.confirm_quotes,
    )


def structure_unrealized_pnl_mid(
    position: PositionState,
    intent: TradeIntent,
    leg_snapshots: Mapping[str, FeatureSnapshot],
) -> Money | None:
    """Mark-to-market structure P&L using mid prices."""
    if not position.legs:
        return None
    leg_by_id = {leg.leg_id: leg for leg in position.legs}
    total = Decimal(0)
    contributed = False
    for intent_leg in intent.legs:
        position_leg = leg_by_id.get(intent_leg.leg_id)
        if position_leg is None:
            continue
        snapshot = _snapshot_for_leg(intent_leg, leg_snapshots)
        if snapshot is None:
            return None
        mid = _mid_price(snapshot)
        if mid is None:
            return None
        contributed = True
        qty = Decimal(position_leg.quantity_contracts)
        entry = position_leg.average_entry_price.value
        if intent_leg.side is Side.BUY:
            leg_pnl = (mid.value - entry) * qty
        else:
            leg_pnl = (entry - mid.value) * qty
        total += leg_pnl
    if not contributed:
        return None
    return Money(total.quantize(Decimal("0.01")), Currency.INR)


def apply_discovery_trailing(
    policy: ExitPolicy,
    *,
    pnl: Money,
    structure_basis: Decimal,
    quantity_contracts: int,
    config: DiscoveryExitsConfig,
) -> ExitPolicy:
    """Tighten structure trail stop from P&L high-water mark; invariant 17."""
    if not policy.premium_scaled or structure_basis <= 0:
        return policy
    activation = (
        structure_basis * Decimal(quantity_contracts) * config.trail_activate_fraction
    ).quantize(Decimal("0.01"))
    if pnl.amount < activation:
        return policy
    hwm = policy.strategy_pnl_hwm
    if hwm is None or pnl.amount > hwm.amount:
        hwm = pnl
    trail_stop_amount = (
        hwm.amount * (Decimal(1) - config.trail_giveback_fraction)
    ).quantize(Decimal("0.01"))
    trail_stop = Money(trail_stop_amount, pnl.currency)
    if policy.pnl_stop is not None and trail_stop.amount < policy.pnl_stop.amount:
        trail_stop = policy.pnl_stop
    existing = policy.pnl_trail_stop
    if existing is not None and trail_stop.amount <= existing.amount:
        if hwm is policy.strategy_pnl_hwm:
            return policy
        return policy.model_copy(update={"strategy_pnl_hwm": hwm})
    return policy.model_copy(
        update={"strategy_pnl_hwm": hwm, "pnl_trail_stop": trail_stop}
    )


def leg_stop_target_breached_mid(
    position: PositionState,
    intent: TradeIntent,
    leg_snapshots: Mapping[str, FeatureSnapshot],
) -> tuple[str, str]:
    """Return exit kind and detail when any leg hits its own stop/target on mid."""
    leg_by_id = {leg.leg_id: leg for leg in position.legs}
    for intent_leg in intent.legs:
        position_leg = leg_by_id.get(intent_leg.leg_id)
        if position_leg is None:
            continue
        snapshot = _snapshot_for_leg(intent_leg, leg_snapshots)
        if snapshot is None:
            continue
        mid = _mid_price(snapshot)
        if mid is None:
            continue
        stop = position_leg.current_stop_price
        if stop is not None and _stop_hit(position_leg.side, mid, stop):
            return (
                "STOP",
                f"leg {position_leg.leg_id} stop price breached",
            )
        target = position_leg.current_target_price
        if target is not None and _target_hit(position_leg.side, mid, target):
            return (
                "TARGET",
                f"leg {position_leg.leg_id} target price reached",
            )
    return "NONE", ""


def evaluate_discovery_confirmation(
    policy: ExitPolicy,
    *,
    exit_kind: str,
    detail: str,
) -> DiscoveryExitEvaluationState:
    """Require consecutive confirming quotes before firing a discovery exit."""
    if exit_kind == "NONE":
        reset = policy.model_copy(
            update={"exit_confirm_count": 0, "pending_exit_kind": None}
        )
        return DiscoveryExitEvaluationState(
            policy=reset,
            exit_kind="NONE",
            detail=detail,
            should_exit=False,
        )
    pending = policy.pending_exit_kind
    count = policy.exit_confirm_count
    if pending == exit_kind:
        count += 1
    else:
        pending = exit_kind
        count = 1
    required = policy.exit_confirm_required
    if count >= required:
        confirmed = policy.model_copy(
            update={
                "exit_confirm_count": count,
                "pending_exit_kind": pending,
            }
        )
        return DiscoveryExitEvaluationState(
            policy=confirmed,
            exit_kind=exit_kind,
            detail=detail,
            should_exit=True,
        )
    waiting = policy.model_copy(
        update={"exit_confirm_count": count, "pending_exit_kind": pending}
    )
    return DiscoveryExitEvaluationState(
        policy=waiting,
        exit_kind="NONE",
        detail=f"{detail} (confirm {count}/{required})",
        should_exit=False,
    )


def _snapshot_for_leg(
    intent_leg: IntentLeg,
    leg_snapshots: Mapping[str, FeatureSnapshot],
) -> FeatureSnapshot | None:
    if intent_leg.leg_id in leg_snapshots:
        return leg_snapshots[intent_leg.leg_id]
    symbol = intent_leg.contract.symbol
    if symbol in leg_snapshots:
        return leg_snapshots[symbol]
    for snap in leg_snapshots.values():
        if snap.contract.symbol == symbol:
            return snap
    return None


def _stop_hit(side: Side, monitor: Price, stop: Price) -> bool:
    if side is Side.BUY:
        return monitor.value <= stop.value
    return monitor.value >= stop.value


def _target_hit(side: Side, monitor: Price, target: Price) -> bool:
    if side is Side.BUY:
        return monitor.value >= target.value
    return monitor.value <= target.value


def _mid_price(feature: FeatureSnapshot) -> Price | None:
    quote = feature.market
    if quote.bid is None or quote.ask is None:
        return None
    tick = quote.bid.tick
    mid_value = (quote.bid.value + quote.ask.value) / Decimal(2)
    return Price.snap(mid_value, tick)


def structure_net_debit_per_unit(legs: tuple[PositionLegState, ...]) -> Decimal:
    """Net debit paid per structure unit (positive = debit, negative = credit)."""
    total = Decimal(0)
    for leg in legs:
        if leg.side is Side.BUY:
            total += leg.average_entry_price.value
        else:
            total -= leg.average_entry_price.value
    return total
