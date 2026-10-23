"""Startup configuration validation (Phase P1 / spec §4, §10.1, G1-G2)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

from trading.config.discovery import (
    DISCOVERY_EXPERIMENT_PREFIX,
    LoadedDiscoveryConfig,
    load_discovery_config,
)
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
from trading.runtime.session_routing import SessionRoutingProfile

if TYPE_CHECKING:
    from trading.runtime.paper_session import PaperSessionConfig


class StartupValidationError(ValueError):
    """Raised when paper session startup configuration violates system gates."""


KNOWN_STRATEGIES_AND_FAMILIES: frozenset[str] = frozenset(
    {f.value for f in FamilyId}
    | {m.value for m in ModeId}
    | {
        "positional_long_option",
        "commodity_futures_trend",
        "commodity_underlying",
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


def _validate_m2_only_session(
    session_config: PaperSessionConfig,
    modes_config: ModesConfig | None,
) -> None:
    """M2-only deployments must run a single PAPER mode with full capital share."""
    if modes_config is None:
        return
    configured = set(modes_config.modes.keys())
    if configured != {ModeId.M2_DIRECTIONAL}:
        raise StartupValidationError(
            "m2_only routing requires modes.yaml to contain only M2_DIRECTIONAL; "
            f"found {sorted(m.value for m in configured)}"
        )
    share = modes_config.modes[ModeId.M2_DIRECTIONAL].capital_share
    if share != Decimal("1"):
        raise StartupValidationError(
            f"m2_only requires M2 capital_share 1.0; got {share}"
        )
    paper_modes = {
        key
        for key, stance in session_config.mode_stances.items()
        if stance is ExecutionMode.PAPER
    }
    if paper_modes != {ModeId.M2_DIRECTIONAL.value}:
        raise StartupValidationError(
            f"m2_only requires exactly M2_DIRECTIONAL PAPER; got {paper_modes!r}"
        )


def _validate_discovery_profile(
    session_config: PaperSessionConfig,
    environment: Environment | None,
    broker: object | None,
    discovery_path: Path | None,
) -> LoadedDiscoveryConfig | None:
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
    """Validate session configuration against Gates G1, G2, and DISCOVERY rules."""
    _ = enforce_g3_shadow
    loaded_discovery = _validate_discovery_profile(
        session_config,
        environment=environment,
        broker=broker,
        discovery_path=discovery_path,
    )
    if loaded_discovery is not None:
        session_config = session_config.model_copy(
            update={"experiment_prefix": DISCOVERY_EXPERIMENT_PREFIX}
        )
    merged_family_stances = effective_family_stances(
        session_config.family_stances,
        entry_profile=session_config.entry_profile,
        discovery_family_stances=(
            loaded_discovery.config.family_stances
            if loaded_discovery is not None
            else None
        ),
    )
    _validate_known_stances(session_config, modes_config)
    _validate_commodity_stances(session_config)
    _validate_budget_and_lifecycle(
        session_config,
        family_stances=merged_family_stances,
    )
    if (
        session_config.routing_profile is SessionRoutingProfile.M2_ONLY
        and modes_config is None
    ):
        raise StartupValidationError(
            "M2 routing requires config/modes.yaml to be present."
        )
    if session_config.routing_profile is SessionRoutingProfile.M2_ONLY:
        _validate_m2_only_session(session_config, modes_config)
    return session_config, []
