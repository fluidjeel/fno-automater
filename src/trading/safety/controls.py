"""Deterministic kill switches, entry freezes and circuit breakers.

Invariant 24: kill-switch actions are independently callable and tested.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum, unique

from trading.config.schema import RiskLimits
from trading.domain.clock import Clock
from trading.domain.contracts.base import (
    NonEmptyStr,
    StrictBool,
    UtcDatetime,
    VersionedModel,
)
from trading.domain.contracts.portfolio import PortfolioSnapshot
from trading.domain.enums import ReasonCode, Trigger
from trading.domain.ids import IdFactory
from trading.domain.primitives import Rounding

__all__ = [
    "SafetyControlEvent",
    "SafetyControlKind",
    "SafetyControls",
    "SafetyControlsError",
    "daily_loss_cap_breached",
]


class SafetyControlsError(Exception):
    """Base error for safety control failures."""


@unique
class SafetyControlKind(StrEnum):
    """Independently invocable safety actions from OPERATIONS_RUNBOOK.md."""

    ENTRY_FREEZE = "ENTRY_FREEZE"
    ENTRY_FREEZE_RELEASE = "ENTRY_FREEZE_RELEASE"
    STRATEGY_HALT = "STRATEGY_HALT"
    STRATEGY_HALT_RELEASE = "STRATEGY_HALT_RELEASE"
    DAILY_LOSS_KILL_SWITCH = "DAILY_LOSS_KILL_SWITCH"
    GLOBAL_HALT = "GLOBAL_HALT"
    GLOBAL_HALT_RELEASE = "GLOBAL_HALT_RELEASE"


class SafetyControlEvent(VersionedModel):
    """Auditable record for one safety control invocation."""

    event_id: NonEmptyStr
    kind: SafetyControlKind
    actor: NonEmptyStr
    scope: NonEmptyStr
    trigger: Trigger
    incident_id: NonEmptyStr | None = None
    prior_entry_frozen: StrictBool
    prior_global_halt: StrictBool
    prior_daily_loss_kill_switch: StrictBool
    prior_halted_strategies: tuple[NonEmptyStr, ...] = ()
    new_entry_frozen: StrictBool
    new_global_halt: StrictBool
    new_daily_loss_kill_switch: StrictBool
    new_halted_strategies: tuple[NonEmptyStr, ...] = ()
    reason_codes: tuple[ReasonCode, ...]
    recorded_at: UtcDatetime


@dataclass(frozen=True, slots=True)
class _SafetyState:
    entry_frozen: bool = False
    global_halt: bool = False
    daily_loss_kill_switch: bool = False
    halted_strategies: frozenset[str] = frozenset()
    protection_degraded: bool = False


class SafetyControls:
    """Track and invoke deterministic safety controls."""

    def __init__(
        self,
        *,
        clock: Clock,
        id_factory: IdFactory,
    ) -> None:
        self._clock = clock
        self._ids = id_factory
        self._state = _SafetyState()

    @property
    def state(self) -> _SafetyState:
        return self._state

    def freeze_entries(
        self,
        *,
        actor: str,
        scope: str,
        incident_id: str | None = None,
        trigger: Trigger = Trigger.OPERATOR,
    ) -> SafetyControlEvent:
        """Block new exposure while continuing management and exits."""
        return self._apply(
            kind=SafetyControlKind.ENTRY_FREEZE,
            actor=actor,
            scope=scope,
            incident_id=incident_id,
            trigger=trigger,
            reason_codes=(ReasonCode.ENTRY_FROZEN,),
            mutate=lambda state: state.__class__(
                entry_frozen=True,
                global_halt=state.global_halt,
                daily_loss_kill_switch=state.daily_loss_kill_switch,
                halted_strategies=state.halted_strategies,
                protection_degraded=state.protection_degraded,
            ),
        )

    def release_entry_freeze(
        self,
        *,
        actor: str,
        scope: str,
        incident_id: str | None = None,
        trigger: Trigger = Trigger.OPERATOR,
    ) -> SafetyControlEvent:
        """Clear an operator entry freeze when recovery checks pass."""
        return self._apply(
            kind=SafetyControlKind.ENTRY_FREEZE_RELEASE,
            actor=actor,
            scope=scope,
            incident_id=incident_id,
            trigger=trigger,
            reason_codes=(ReasonCode.OK,),
            mutate=lambda state: state.__class__(
                entry_frozen=False,
                global_halt=state.global_halt,
                daily_loss_kill_switch=state.daily_loss_kill_switch,
                halted_strategies=state.halted_strategies,
                protection_degraded=state.protection_degraded,
            ),
        )

    def halt_strategy(
        self,
        strategy_id: str,
        *,
        actor: str,
        scope: str,
        incident_id: str | None = None,
        trigger: Trigger = Trigger.OPERATOR,
    ) -> SafetyControlEvent:
        """Freeze one strategy's new intents."""
        return self._apply(
            kind=SafetyControlKind.STRATEGY_HALT,
            actor=actor,
            scope=scope,
            incident_id=incident_id,
            trigger=trigger,
            reason_codes=(ReasonCode.STRATEGY_HALTED,),
            mutate=lambda state: state.__class__(
                entry_frozen=state.entry_frozen,
                global_halt=state.global_halt,
                daily_loss_kill_switch=state.daily_loss_kill_switch,
                halted_strategies=state.halted_strategies | {strategy_id},
                protection_degraded=state.protection_degraded,
            ),
        )

    def release_strategy_halt(
        self,
        strategy_id: str,
        *,
        actor: str,
        scope: str,
        incident_id: str | None = None,
        trigger: Trigger = Trigger.OPERATOR,
    ) -> SafetyControlEvent:
        """Resume new intents for one strategy."""
        return self._apply(
            kind=SafetyControlKind.STRATEGY_HALT_RELEASE,
            actor=actor,
            scope=scope,
            incident_id=incident_id,
            trigger=trigger,
            reason_codes=(ReasonCode.OK,),
            mutate=lambda state: state.__class__(
                entry_frozen=state.entry_frozen,
                global_halt=state.global_halt,
                daily_loss_kill_switch=state.daily_loss_kill_switch,
                halted_strategies=state.halted_strategies - {strategy_id},
                protection_degraded=state.protection_degraded,
            ),
        )

    def activate_global_halt(
        self,
        *,
        actor: str,
        scope: str,
        incident_id: str | None = None,
        trigger: Trigger = Trigger.OPERATOR,
    ) -> SafetyControlEvent:
        """Block all new exposure and require recovery checks."""
        return self._apply(
            kind=SafetyControlKind.GLOBAL_HALT,
            actor=actor,
            scope=scope,
            incident_id=incident_id,
            trigger=trigger,
            reason_codes=(ReasonCode.KILL_SWITCH_ACTIVE,),
            mutate=lambda state: state.__class__(
                entry_frozen=True,
                global_halt=True,
                daily_loss_kill_switch=state.daily_loss_kill_switch,
                halted_strategies=state.halted_strategies,
                protection_degraded=state.protection_degraded,
            ),
        )

    def release_global_halt(
        self,
        *,
        actor: str,
        scope: str,
        incident_id: str | None = None,
        trigger: Trigger = Trigger.OPERATOR,
    ) -> SafetyControlEvent:
        """Clear a global halt after recovery checks pass."""
        return self._apply(
            kind=SafetyControlKind.GLOBAL_HALT_RELEASE,
            actor=actor,
            scope=scope,
            incident_id=incident_id,
            trigger=trigger,
            reason_codes=(ReasonCode.OK,),
            mutate=lambda state: state.__class__(
                entry_frozen=False,
                global_halt=False,
                daily_loss_kill_switch=state.daily_loss_kill_switch,
                halted_strategies=state.halted_strategies,
                protection_degraded=state.protection_degraded,
            ),
        )

    def evaluate_daily_loss_kill_switch(
        self,
        portfolio: PortfolioSnapshot,
        account_risk: RiskLimits,
        *,
        scope: str,
        trigger: Trigger = Trigger.SCHEDULER,
    ) -> SafetyControlEvent | None:
        """Latch the daily-loss kill switch when the account cap is breached."""
        if self._state.daily_loss_kill_switch:
            return None
        if not daily_loss_cap_breached(portfolio, account_risk):
            return None
        return self._apply(
            kind=SafetyControlKind.DAILY_LOSS_KILL_SWITCH,
            actor="risk-engine",
            scope=scope,
            trigger=trigger,
            reason_codes=(
                ReasonCode.RISK_LIMIT_DAILY_LOSS,
                ReasonCode.KILL_SWITCH_ACTIVE,
            ),
            mutate=lambda state: state.__class__(
                entry_frozen=True,
                global_halt=state.global_halt,
                daily_loss_kill_switch=True,
                halted_strategies=state.halted_strategies,
                protection_degraded=state.protection_degraded,
            ),
        )

    def blocks_entry(self, strategy_id: str | None = None) -> bool:
        """Return whether new exposure is blocked by an active control."""
        return bool(self.entry_block_reasons(strategy_id))

    def entry_block_reasons(
        self,
        strategy_id: str | None = None,
    ) -> tuple[ReasonCode, ...]:
        """Return machine-readable reasons blocking new exposure."""
        reasons: list[ReasonCode] = []
        if self._state.global_halt or self._state.daily_loss_kill_switch:
            reasons.append(ReasonCode.KILL_SWITCH_ACTIVE)
        if self._state.entry_frozen:
            reasons.append(ReasonCode.ENTRY_FROZEN)
        if self._state.protection_degraded:
            reasons.append(ReasonCode.PROTECTION_DEGRADED)
        if strategy_id is not None and strategy_id in self._state.halted_strategies:
            reasons.append(ReasonCode.STRATEGY_HALTED)
        return tuple(dict.fromkeys(reasons))

    def degrade_protection(
        self,
        *,
        actor: str,
        scope: str,
        incident_id: str | None = None,
        trigger: Trigger = Trigger.OPERATOR,
    ) -> SafetyControlEvent:
        """Block new entries when software protection monitoring is unreliable."""
        return self._apply(
            kind=SafetyControlKind.ENTRY_FREEZE,
            actor=actor,
            scope=scope,
            incident_id=incident_id,
            trigger=trigger,
            reason_codes=(ReasonCode.PROTECTION_DEGRADED,),
            mutate=lambda state: state.__class__(
                entry_frozen=state.entry_frozen,
                global_halt=state.global_halt,
                daily_loss_kill_switch=state.daily_loss_kill_switch,
                halted_strategies=state.halted_strategies,
                protection_degraded=True,
            ),
        )

    def restore_protection(
        self,
        *,
        actor: str,
        scope: str,
        incident_id: str | None = None,
        trigger: Trigger = Trigger.OPERATOR,
    ) -> SafetyControlEvent:
        """Clear protection degradation after fresh quotes return."""
        return self._apply(
            kind=SafetyControlKind.ENTRY_FREEZE_RELEASE,
            actor=actor,
            scope=scope,
            incident_id=incident_id,
            trigger=trigger,
            reason_codes=(ReasonCode.OK,),
            mutate=lambda state: state.__class__(
                entry_frozen=state.entry_frozen,
                global_halt=state.global_halt,
                daily_loss_kill_switch=state.daily_loss_kill_switch,
                halted_strategies=state.halted_strategies,
                protection_degraded=False,
            ),
        )

    def _apply(
        self,
        *,
        kind: SafetyControlKind,
        actor: str,
        scope: str,
        trigger: Trigger,
        reason_codes: tuple[ReasonCode, ...],
        mutate: Callable[[_SafetyState], _SafetyState],
        incident_id: str | None = None,
    ) -> SafetyControlEvent:
        prior = self._state
        new_state = mutate(prior)
        self._state = new_state
        return SafetyControlEvent(
            event_id=self._ids.new_id("SAFE"),
            kind=kind,
            actor=actor,
            scope=scope,
            trigger=trigger,
            incident_id=incident_id,
            prior_entry_frozen=prior.entry_frozen,
            prior_global_halt=prior.global_halt,
            prior_daily_loss_kill_switch=prior.daily_loss_kill_switch,
            prior_halted_strategies=tuple(sorted(prior.halted_strategies)),
            new_entry_frozen=new_state.entry_frozen,
            new_global_halt=new_state.global_halt,
            new_daily_loss_kill_switch=new_state.daily_loss_kill_switch,
            new_halted_strategies=tuple(sorted(new_state.halted_strategies)),
            reason_codes=reason_codes,
            recorded_at=self._clock.now_utc(),
        )


def daily_loss_cap_breached(
    portfolio: PortfolioSnapshot,
    account_risk: RiskLimits,
) -> bool:
    """Return True when realized day loss has consumed the configured cap."""
    realized = portfolio.exposure.realized_pnl_today
    if not realized.is_negative:
        return False
    daily_cap = (
        portfolio.exposure.equity * account_risk.daily_loss_cap_fraction
    ).quantized(Rounding.FLOOR)
    loss = (-realized).quantized(Rounding.FLOOR)
    return loss >= daily_cap
