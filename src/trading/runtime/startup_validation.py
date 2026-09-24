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
from trading.runtime.cas_event_path import measured_report_passes
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
    """Record M1 latency limitations for PAPER. Stances are not demoted here."""
    _ = enforce_g3_shadow
    warnings: list[str] = []
    cas_event_enabled = session_config.cas_event_driven.enabled
    report_ok = measured_report_passes(session_config.cas_event_driven)
    is_slow_loop = (
        session_config.poll_interval_seconds >= _MIN_CAS_POLL_INTERVAL_SECONDS
    )
    for cas_key in ("cas_microstructure", "M1_CAS"):
        is_paper = (
            session_config.strategy_stances.get(cas_key) is ExecutionMode.PAPER
            or session_config.mode_stances.get(cas_key) is ExecutionMode.PAPER
        )
        if not is_paper or not is_slow_loop:
            continue
        if not cas_event_enabled:
            warnings.append(
                f"M1 ({cas_key}) PAPER on a {session_config.poll_interval_seconds}s "
                "poll: cas_event_driven is disabled, so the poll does not submit "
                "M1; use submit_m1_event for entries."
            )
            continue
        if not report_ok:
            warnings.append(
                f"M1 ({cas_key}) PAPER: oracle-measured latency report is missing "
                "or above the predeclared thresholds. PAPER continues; latency "
                "is a measured limitation, not a promotion gate."
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
