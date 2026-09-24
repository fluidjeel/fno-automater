"""Startup configuration validation (Phase P1 / spec §4, §10.1, G1-G3)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from trading.domain.contracts.mode_policy import ModesConfig
from trading.domain.enums import ExecutionMode, FamilyId, ModeId
from trading.domain.family_gates import (
    CALENDAR_FAMILIES,
    G1_EXCEEDS_BUDGET_FAMILIES,
    G2_UNPROVEN_FAMILIES,
)
from trading.runtime.session_routing import SessionRoutingProfile

if TYPE_CHECKING:
    from trading.runtime.paper_session import PaperSessionConfig


class StartupValidationError(ValueError):
    """Raised when paper session startup configuration violates system gates."""


_MIN_CAS_POLL_INTERVAL_SECONDS = 60

KNOWN_STRATEGIES_AND_FAMILIES: frozenset[str] = frozenset(
    {f.value for f in FamilyId}
    | {m.value for m in ModeId}
    | {
        "positional_long_option",
        "debit_spread",
        "credit_spread",
        "defined_risk_multileg",
        "cas_microstructure",
        "commodity_futures_trend",
        "commodity_underlying",
        "iron_condor",
    }
)


def _validate_known_stances(
    session_config: PaperSessionConfig,
    modes_config: ModesConfig | None,
) -> None:
    known = set(KNOWN_STRATEGIES_AND_FAMILIES) | set(session_config.strategy_ids)
    if modes_config is not None:
        known |= {m.value for m in modes_config.modes}
        for mode in modes_config.modes.values():
            known |= {f.value for f in mode.allowed_families}

    for key in (
        *session_config.strategy_stances,
        *session_config.mode_stances,
        *session_config.family_stances,
    ):
        if key not in known:
            raise StartupValidationError(
                f"Unknown family, mode, or strategy in session stances: {key!r}"
            )


def _validate_commodity_stances(session_config: PaperSessionConfig) -> None:
    commodity_paper = (
        session_config.strategy_stances.get("commodity_underlying")
        is ExecutionMode.PAPER
        or session_config.strategy_stances.get("commodity_futures_trend")
        is ExecutionMode.PAPER
    )
    if commodity_paper:
        raise StartupValidationError(
            "Commodity futures execution is disabled on NIFTY paper deployment; "
            "stance must be SHADOW."
        )


def _paper_stance_for_family(
    session_config: PaperSessionConfig, family: str
) -> ExecutionMode | None:
    if session_config.family_stances.get(family) is ExecutionMode.PAPER:
        return ExecutionMode.PAPER
    if session_config.strategy_stances.get(family) is ExecutionMode.PAPER:
        return ExecutionMode.PAPER
    return None


def _validate_budget_and_lifecycle(session_config: PaperSessionConfig) -> None:
    for family in G1_EXCEEDS_BUDGET_FAMILIES:
        if _paper_stance_for_family(session_config, family) is ExecutionMode.PAPER:
            raise StartupValidationError(
                f"Gate G1 violation: Family '{family}' exceeds budget "
                "(MIN_LOT_EXCEEDS_BUDGET) and cannot run PAPER."
            )

    for family in CALENDAR_FAMILIES:
        if _paper_stance_for_family(session_config, family) is ExecutionMode.PAPER:
            raise StartupValidationError(
                f"Calendar family '{family}' is "
                "EXPERIMENTAL_ONLY_RISK_BOUND_UNPROVEN and cannot run PAPER "
                "on the strict book."
            )

    for family in G2_UNPROVEN_FAMILIES:
        if _paper_stance_for_family(session_config, family) is ExecutionMode.PAPER:
            raise StartupValidationError(
                f"Gate G2 violation: Family '{family}' is not lifecycle proven "
                "(LIFECYCLE_PROVEN) and cannot run PAPER. Stance must be SHADOW."
            )


def _validate_g3_cas_loop(
    session_config: PaperSessionConfig,
    *,
    enforce_g3_shadow: bool,
) -> tuple[PaperSessionConfig, list[str]]:
    warnings: list[str] = []
    new_stances = dict(session_config.strategy_stances)
    demoted = False

    new_mode_stances = dict(session_config.mode_stances)
    cas_event_enabled = session_config.cas_event_driven.enabled
    for cas_key in ("cas_microstructure", "M1_CAS"):
        is_paper = (
            session_config.strategy_stances.get(cas_key) is ExecutionMode.PAPER
            or new_mode_stances.get(cas_key) is ExecutionMode.PAPER
        )
        is_slow_loop = (
            session_config.poll_interval_seconds >= _MIN_CAS_POLL_INTERVAL_SECONDS
        )
        if is_paper and is_slow_loop and not cas_event_enabled:
            if enforce_g3_shadow:
                if cas_key in new_stances:
                    new_stances[cas_key] = ExecutionMode.SHADOW
                if cas_key in new_mode_stances:
                    new_mode_stances[cas_key] = ExecutionMode.SHADOW
                demoted = True
                warnings.append(
                    f"Gate G3 enforcement: Demoted {cas_key} from PAPER to SHADOW "
                    f"because poll_interval_seconds="
                    f"{session_config.poll_interval_seconds} >= "
                    f"{_MIN_CAS_POLL_INTERVAL_SECONDS} and cas_event_driven "
                    "is disabled."
                )
            else:
                raise StartupValidationError(
                    "Gate G3 violation: Mode 1 (cas_microstructure) cannot run "
                    "PAPER on a polled loop with poll_interval_seconds="
                    f"{session_config.poll_interval_seconds} (BLOCKED). "
                    "Enable cas_event_driven or set stance SHADOW."
                )

    if demoted:
        session_config = session_config.model_copy(
            update={
                "strategy_stances": new_stances,
                "mode_stances": new_mode_stances,
            }
        )

    return session_config, warnings


def validate_startup_configuration(
    session_config: PaperSessionConfig,
    modes_config: ModesConfig | None = None,
    *,
    enforce_g3_shadow: bool = False,
) -> tuple[PaperSessionConfig, list[str]]:
    """Validate session configuration against Gates G1, G2, G3 and NIFTY rules."""
    _validate_known_stances(session_config, modes_config)
    _validate_commodity_stances(session_config)
    _validate_budget_and_lifecycle(session_config)
    if (
        session_config.routing_profile is SessionRoutingProfile.FOUR_MODE
        and modes_config is None
    ):
        raise StartupValidationError(
            "Four-mode routing requires config/modes.yaml to be present."
        )
    return _validate_g3_cas_loop(
        session_config,
        enforce_g3_shadow=enforce_g3_shadow,
    )
