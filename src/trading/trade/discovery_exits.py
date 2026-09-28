"""DISCOVERY-only premium-scaled exit policy construction and evaluation."""

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
from trading.risk.sizing.credit_spread import is_credit_spread
from trading.risk.sizing.debit_spread import is_debit_spread

__all__ = [
    "DiscoveryExitEvaluationState",
    "apply_discovery_trailing",
    "build_discovery_exit_policy",
    "compute_leg_disaster_stop",
    "evaluate_discovery_confirmation",
    "is_premium_scaled_policy",
    "leg_disaster_stop_breached",
    "structure_is_credit",
    "structure_net_debit_per_unit",
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


def structure_net_debit_per_unit(
    legs: tuple[PositionLegState, ...],
) -> Decimal:
    """Net debit paid per structure unit (positive = debit, negative = credit)."""
    total = Decimal(0)
    for leg in legs:
        cash = leg.average_entry_price.value
        if leg.side is Side.BUY:
            total += cash
        else:
            total -= cash
    return total


def structure_is_credit(
    intent: TradeIntent, legs: tuple[PositionLegState, ...]
) -> bool:
    """Classify structure as net credit for exit policy construction."""
    if is_credit_spread(intent):
        return True
    if is_debit_spread(intent):
        return False
    per_unit = structure_net_debit_per_unit(legs)
    return per_unit < 0


def compute_leg_disaster_stop(
    *,
    entry_price: Price,
    side: Side,
    config: DiscoveryExitsConfig,
) -> Price:
    """Per-leg disaster backstop; never copied from another leg."""
    tick = entry_price.tick
    if side is Side.BUY:
        retained = Decimal(1) - config.long_leg_disaster_fraction
        stop_value = entry_price.value * retained
        return Price.snap(stop_value, tick, rounding=Rounding.FLOOR)
    multiplier = config.short_leg_disaster_multiplier
    stop_value = entry_price.value * multiplier
    return Price.snap(stop_value, tick, rounding=Rounding.CEILING)


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
    quantity_contracts: int,
    max_loss: Money | None = None,
) -> ExitPolicy:
    """Build a frozen premium-scaled exit policy for DISCOVERY PAPER entries."""
    if not legs:
        raise ValueError("discovery exit policy requires at least one leg")
    qty = Decimal(quantity_contracts)
    per_unit = structure_net_debit_per_unit(legs)
    currency = Currency.INR
    is_credit = structure_is_credit(intent, legs)
    basis_per_unit = abs(per_unit)
    pnl_stop: Money | None = None
    pnl_target: Money | None = None
    if is_credit:
        credit = basis_per_unit * qty
        target_amount = (credit * config.credit_take_profit_fraction).quantize(
            Decimal("0.01")
        )
        pnl_target = Money(target_amount, currency)
        raw_stop = credit * config.credit_stop_multiplier
        if max_loss is not None:
            raw_stop = min(raw_stop, max_loss.amount)
        pnl_stop = Money((-raw_stop).quantize(Decimal("0.01")), currency)
    else:
        debit = basis_per_unit * qty
        stop_amount = (debit * config.debit_stop_fraction).quantize(Decimal("0.01"))
        pnl_stop = Money(-stop_amount, currency)
        target_amount = (debit * config.debit_target_fraction).quantize(Decimal("0.01"))
        pnl_target = Money(target_amount, currency)
    return ExitPolicy(
        policy_id=policy_id,
        trade_id=trade_id,
        scope=scope,
        initial_stop_distance_ticks=1,
        current_stop_distance_ticks=1,
        stop_price=None,
        target_price=None,
        strategy_entry_pnl=Money(Decimal(0), currency),
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
    per_unit_debit: Decimal,
    quantity_contracts: int,
    config: DiscoveryExitsConfig,
) -> ExitPolicy:
    """Tighten structure trail stop from P&L high-water mark; invariant 17."""
    if not policy.premium_scaled or per_unit_debit <= 0:
        return policy
    activation = (
        per_unit_debit * Decimal(quantity_contracts) * config.trail_activation_fraction
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
    existing = policy.pnl_trail_stop
    if existing is not None and trail_stop.amount <= existing.amount:
        if hwm is policy.strategy_pnl_hwm:
            return policy
        return policy.model_copy(update={"strategy_pnl_hwm": hwm})
    return policy.model_copy(
        update={"strategy_pnl_hwm": hwm, "pnl_trail_stop": trail_stop}
    )


def leg_disaster_stop_breached(
    position: PositionState,
    intent: TradeIntent,
    leg_snapshots: Mapping[str, FeatureSnapshot],
) -> tuple[bool, str]:
    """Return whether any open leg hit its per-leg disaster backstop on mid."""
    leg_by_id = {leg.leg_id: leg for leg in position.legs}
    for intent_leg in intent.legs:
        position_leg = leg_by_id.get(intent_leg.leg_id)
        if position_leg is None:
            continue
        stop = position_leg.current_stop_price
        if stop is None:
            continue
        snapshot = _snapshot_for_leg(intent_leg, leg_snapshots)
        if snapshot is None:
            continue
        mid = _mid_price(snapshot)
        if mid is None:
            continue
        if _stop_hit(position_leg.side, mid, stop):
            return True, f"leg {position_leg.leg_id} disaster backstop breached"
    return False, ""


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


def _mid_price(feature: FeatureSnapshot) -> Price | None:
    quote = feature.market
    if quote.bid is None or quote.ask is None:
        return None
    tick = quote.bid.tick
    mid_value = (quote.bid.value + quote.ask.value) / Decimal(2)
    return Price.snap(mid_value, tick)
