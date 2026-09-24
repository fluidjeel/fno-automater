"""Deterministic kill switches, entry freezes and circuit breakers.

Invariant 24: kill-switch actions are independently callable and tested.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum, unique
from typing import Any

from trading.config.schema import RiskLimits
from trading.domain.clock import Clock
from trading.domain.contracts.base import (
    NonEmptyStr,
    StrictBool,
    UtcDatetime,
    VersionedModel,
)
from trading.domain.contracts.portfolio import PortfolioSnapshot
from trading.domain.enums import ModeId, ReasonCode, Trigger
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
    MODE_HALT = "MODE_HALT"
    MODE_HALT_RELEASE = "MODE_HALT_RELEASE"
    MODE_DAILY_LOSS_KILL_SWITCH = "MODE_DAILY_LOSS_KILL_SWITCH"
    MODE_ENTRY_FREEZE = "MODE_ENTRY_FREEZE"
    MODE_ENTRY_FREEZE_RELEASE = "MODE_ENTRY_FREEZE_RELEASE"


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
    # Mode-level state snapshots
    prior_halted_modes: tuple[str, ...] = ()
    new_halted_modes: tuple[str, ...] = ()
    prior_mode_daily_loss_switches: tuple[str, ...] = ()
    new_mode_daily_loss_switches: tuple[str, ...] = ()
    prior_mode_entry_frozen: tuple[str, ...] = ()
    new_mode_entry_frozen: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _SafetyState:
    entry_frozen: bool = False
    global_halt: bool = False
    daily_loss_kill_switch: bool = False
    halted_strategies: frozenset[str] = frozenset()
    protection_degraded: bool = False
    halted_modes: frozenset[ModeId] = frozenset()
    mode_daily_loss_switches: frozenset[ModeId] = frozenset()
    mode_entry_frozen: frozenset[ModeId] = frozenset()


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
                halted_modes=state.halted_modes,
                mode_daily_loss_switches=state.mode_daily_loss_switches,
                mode_entry_frozen=state.mode_entry_frozen,
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
                halted_modes=state.halted_modes,
                mode_daily_loss_switches=state.mode_daily_loss_switches,
                mode_entry_frozen=state.mode_entry_frozen,
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
                halted_modes=state.halted_modes,
                mode_daily_loss_switches=state.mode_daily_loss_switches,
                mode_entry_frozen=state.mode_entry_frozen,
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
                halted_modes=state.halted_modes,
                mode_daily_loss_switches=state.mode_daily_loss_switches,
                mode_entry_frozen=state.mode_entry_frozen,
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
                halted_modes=state.halted_modes,
                mode_daily_loss_switches=state.mode_daily_loss_switches,
                mode_entry_frozen=state.mode_entry_frozen,
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
                halted_modes=state.halted_modes,
                mode_daily_loss_switches=state.mode_daily_loss_switches,
                mode_entry_frozen=state.mode_entry_frozen,
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
                halted_modes=state.halted_modes,
                mode_daily_loss_switches=state.mode_daily_loss_switches,
                mode_entry_frozen=state.mode_entry_frozen,
            ),
        )

    def freeze_mode_entries(
        self,
        mode_id: ModeId,
        *,
        actor: str,
        scope: str,
        incident_id: str | None = None,
        trigger: Trigger = Trigger.OPERATOR,
    ) -> SafetyControlEvent:
        """Freeze new entries for one mode without affecting other modes."""
        return self._apply_mode(
            kind=SafetyControlKind.MODE_ENTRY_FREEZE,
            mode_id=mode_id,
            actor=actor,
            scope=scope,
            incident_id=incident_id,
            trigger=trigger,
            reason_codes=(ReasonCode.ENTRY_FROZEN,),
            mutate=lambda state: dataclasses.replace(
                state,
                mode_entry_frozen=state.mode_entry_frozen | {mode_id},
            ),
        )

    def release_mode_entry_freeze(
        self,
        mode_id: ModeId,
        *,
        actor: str,
        scope: str,
        incident_id: str | None = None,
        trigger: Trigger = Trigger.OPERATOR,
    ) -> SafetyControlEvent:
        """Release a per-mode entry freeze."""
        return self._apply_mode(
            kind=SafetyControlKind.MODE_ENTRY_FREEZE_RELEASE,
            mode_id=mode_id,
            actor=actor,
            scope=scope,
            incident_id=incident_id,
            trigger=trigger,
            reason_codes=(ReasonCode.OK,),
            mutate=lambda state: dataclasses.replace(
                state,
                mode_entry_frozen=state.mode_entry_frozen - {mode_id},
            ),
        )

    def halt_mode(
        self,
        mode_id: ModeId,
        *,
        actor: str,
        scope: str,
        incident_id: str | None = None,
        trigger: Trigger = Trigger.OPERATOR,
    ) -> SafetyControlEvent:
        """Halt all new entries for a mode and add it to the halted set."""
        return self._apply_mode(
            kind=SafetyControlKind.MODE_HALT,
            mode_id=mode_id,
            actor=actor,
            scope=scope,
            incident_id=incident_id,
            trigger=trigger,
            reason_codes=(ReasonCode.STRATEGY_HALTED,),
            mutate=lambda state: dataclasses.replace(
                state,
                halted_modes=state.halted_modes | {mode_id},
                mode_entry_frozen=state.mode_entry_frozen | {mode_id},
            ),
        )

    def release_mode_halt(
        self,
        mode_id: ModeId,
        *,
        actor: str,
        scope: str,
        incident_id: str | None = None,
        trigger: Trigger = Trigger.OPERATOR,
    ) -> SafetyControlEvent:
        """Release a per-mode halt."""
        return self._apply_mode(
            kind=SafetyControlKind.MODE_HALT_RELEASE,
            mode_id=mode_id,
            actor=actor,
            scope=scope,
            incident_id=incident_id,
            trigger=trigger,
            reason_codes=(ReasonCode.OK,),
            mutate=lambda state: dataclasses.replace(
                state,
                halted_modes=state.halted_modes - {mode_id},
                mode_entry_frozen=state.mode_entry_frozen - {mode_id},
            ),
        )

    def evaluate_mode_daily_loss(
        self,
        mode_id: ModeId,
        ledger: Any,  # ModeLedger (use Any to avoid circular import)
        *,
        scope: str,
        trigger: Trigger = Trigger.SCHEDULER,
        daily_budget_cap_fraction: Any = None,  # Decimal
    ) -> SafetyControlEvent | None:
        """Latch mode daily-loss kill switch when the mode cap is breached."""
        if mode_id in self._state.mode_daily_loss_switches:
            return None
        if daily_budget_cap_fraction is None:
            return None
        if not ledger.daily_loss_breached(daily_budget_cap_fraction):
            return None
        return self._apply_mode(
            kind=SafetyControlKind.MODE_DAILY_LOSS_KILL_SWITCH,
            mode_id=mode_id,
            actor="risk-engine",
            scope=scope,
            trigger=trigger,
            reason_codes=(
                ReasonCode.RISK_LIMIT_DAILY_LOSS,
                ReasonCode.ENTRY_FROZEN,
            ),
            mutate=lambda state: dataclasses.replace(
                state,
                mode_daily_loss_switches=state.mode_daily_loss_switches | {mode_id},
                mode_entry_frozen=state.mode_entry_frozen | {mode_id},
            ),
        )

    def blocks_entry(
        self,
        strategy_id: str | None = None,
        mode_id: ModeId | None = None,
    ) -> bool:
        """Return whether new exposure is blocked by an active control."""
        return bool(self.entry_block_reasons(strategy_id, mode_id))

    def entry_block_reasons(
        self,
        strategy_id: str | None = None,
        mode_id: ModeId | None = None,
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
        if mode_id is not None:
            if mode_id in self._state.mode_daily_loss_switches:
                reasons.append(ReasonCode.RISK_LIMIT_DAILY_LOSS)
                reasons.append(ReasonCode.ENTRY_FROZEN)
            if mode_id in self._state.halted_modes:
                reasons.append(ReasonCode.STRATEGY_HALTED)
            if mode_id in self._state.mode_entry_frozen:
                reasons.append(ReasonCode.ENTRY_FROZEN)
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
                halted_modes=state.halted_modes,
                mode_daily_loss_switches=state.mode_daily_loss_switches,
                mode_entry_frozen=state.mode_entry_frozen,
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
                halted_modes=state.halted_modes,
                mode_daily_loss_switches=state.mode_daily_loss_switches,
                mode_entry_frozen=state.mode_entry_frozen,
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
            prior_halted_modes=tuple(sorted(m.value for m in prior.halted_modes)),
            new_halted_modes=tuple(sorted(m.value for m in new_state.halted_modes)),
            prior_mode_daily_loss_switches=tuple(
                sorted(m.value for m in prior.mode_daily_loss_switches)
            ),
            new_mode_daily_loss_switches=tuple(
                sorted(m.value for m in new_state.mode_daily_loss_switches)
            ),
            prior_mode_entry_frozen=tuple(
                sorted(m.value for m in prior.mode_entry_frozen)
            ),
            new_mode_entry_frozen=tuple(
                sorted(m.value for m in new_state.mode_entry_frozen)
            ),
        )

    def _apply_mode(
        self,
        *,
        kind: SafetyControlKind,
        mode_id: ModeId,
        actor: str,
        scope: str,
        trigger: Trigger,
        reason_codes: tuple[ReasonCode, ...],
        mutate: Callable[[_SafetyState], _SafetyState],
        incident_id: str | None = None,
    ) -> SafetyControlEvent:
        """Apply a mode-scoped state mutation and return an audit event."""
        return self._apply(
            kind=kind,
            actor=actor,
            scope=scope,
            trigger=trigger,
            reason_codes=reason_codes,
            mutate=mutate,
            incident_id=incident_id,
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
