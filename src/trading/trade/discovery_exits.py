"""DISCOVERY-only per-leg and structure exit policy construction."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from trading.config.discovery import DiscoveryExitsConfig
from trading.domain.contracts.intent import ExitTemplate, TradeIntent
from trading.domain.contracts.position import ExitPolicy, PositionLegState
from trading.domain.enums import ExitScope, Side
from trading.domain.primitives import Currency, Money, Price, Rounding

__all__ = [
    "apply_discovery_leg_exit_prices",
    "build_discovery_exit_policy",
    "compute_leg_stop_price",
    "compute_leg_target_price",
    "is_premium_scaled_policy",
    "structure_pnl_bounds",
]


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
    )


def structure_net_debit_per_unit(legs: tuple[PositionLegState, ...]) -> Decimal:
    """Net debit paid per structure unit (positive = debit, negative = credit)."""
    total = Decimal(0)
    for leg in legs:
        if leg.side is Side.BUY:
            total += leg.average_entry_price.value
        else:
            total -= leg.average_entry_price.value
    return total
