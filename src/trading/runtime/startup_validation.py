"""Startup configuration validation (Phase P1 / spec §4, §10.1, G1-G3)."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from trading.config.discovery import DiscoveryConfig, load_discovery_config
from trading.domain.contracts.mode_policy import ModesConfig
from trading.domain.enums import (
    EntryProfile,
    Environment,
    ExecutionMode,
    FamilyId,
    ModeId,
)
from trading.domain.family_gates import (
    CALENDAR_FAMILIES,
    G1_EXCEEDS_BUDGET_FAMILIES,
    G2_LEGACY_UNPROVEN_STRATEGY_IDS,
    G2_UNPROVEN_FAMILIES,
    effective_family_stances,
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
    session_config: PaperSessionConfig,
    family: str,
    *,
    family_stances: dict[str, ExecutionMode],
) -> ExecutionMode | None:
    if family_stances.get(family) is ExecutionMode.PAPER:
        return ExecutionMode.PAPER
    if session_config.strategy_stances.get(family) is ExecutionMode.PAPER:
        return ExecutionMode.PAPER
    return None


def _validate_budget_and_lifecycle(
    session_config: PaperSessionConfig,
    *,
    family_stances: dict[str, ExecutionMode],
) -> None:
    discovery_soft = session_config.entry_profile is EntryProfile.DISCOVERY

    if not discovery_soft:
        for family in G1_EXCEEDS_BUDGET_FAMILIES:
            if (
                _paper_stance_for_family(
                    session_config, family, family_stances=family_stances
                )
                is ExecutionMode.PAPER
            ):
                raise StartupValidationError(
                    f"Gate G1 violation: Family '{family}' exceeds budget "
                    "(MIN_LOT_EXCEEDS_BUDGET) and cannot run PAPER."
                )

    for family in CALENDAR_FAMILIES:
        if (
            _paper_stance_for_family(
                session_config, family, family_stances=family_stances
            )
            is ExecutionMode.PAPER
        ):
            raise StartupValidationError(
                f"Calendar family '{family}' is "
                "EXPERIMENTAL_ONLY_RISK_BOUND_UNPROVEN and cannot run PAPER "
                "on the strict book."
            )

    if not discovery_soft:
        for family in G2_UNPROVEN_FAMILIES:
            if (
                _paper_stance_for_family(
                    session_config, family, family_stances=family_stances
                )
                is ExecutionMode.PAPER
            ):
                raise StartupValidationError(
                    f"Gate G2 violation: Family '{family}' is not lifecycle proven "
                    "(LIFECYCLE_PROVEN) and cannot run PAPER. Stance must be SHADOW."
                )

    for family in G2_LEGACY_UNPROVEN_STRATEGY_IDS:
        if (
            _paper_stance_for_family(
                session_config, family, family_stances=family_stances
            )
            is ExecutionMode.PAPER
        ):
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


def _validate_discovery_profile(
    session_config: PaperSessionConfig,
    environment: Environment | None,
    broker: object | None,
    discovery_path: Path | None,
) -> DiscoveryConfig | None:
    if session_config.entry_profile is not EntryProfile.DISCOVERY:
        return None
    if environment is not None and environment is not Environment.PAPER:
        raise StartupValidationError(
            f"DISCOVERY entry profile cannot run under Environment.{environment.name}; "
            "only Environment.PAPER is permitted."
        )
    if broker is not None:
        broker_mod = type(broker).__module__
        if any(
            broker_mod == prefix or broker_mod.startswith(f"{prefix}.")
            for prefix in ("trading.broker.fyers",)
        ) or getattr(broker, "is_live", False):
            raise StartupValidationError(
                f"{type(broker).__name__} is a real broker adapter; "
                "DISCOVERY entry profile requires the paper broker."
            )
    disc_path = discovery_path or Path("config/discovery.yaml")
    if not disc_path.is_file():
        raise StartupValidationError(
            f"DISCOVERY entry profile requires {disc_path} to exist."
        )
    try:
        return load_discovery_config(disc_path)
    except Exception as exc:
        raise StartupValidationError(
            f"Malformed discovery configuration in {disc_path}: {exc}"
        ) from exc


def validate_startup_configuration(
    session_config: PaperSessionConfig,
    modes_config: ModesConfig | None = None,
    *,
    enforce_g3_shadow: bool = False,
    environment: Environment | None = None,
    broker: object | None = None,
    discovery_path: Path | None = None,
) -> tuple[PaperSessionConfig, list[str]]:
    """Validate session configuration against Gates G1, G2, G3, DISCOVERY rules."""
    discovery_config = _validate_discovery_profile(
        session_config,
        environment=environment,
        broker=broker,
        discovery_path=discovery_path,
    )
    merged_family_stances = effective_family_stances(
        session_config.family_stances,
        entry_profile=session_config.entry_profile,
        discovery_family_stances=(
            discovery_config.family_stances if discovery_config is not None else None
        ),
    )
    _validate_known_stances(session_config, modes_config)
    _validate_commodity_stances(session_config)
    _validate_budget_and_lifecycle(
        session_config,
        family_stances=merged_family_stances,
    )
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
