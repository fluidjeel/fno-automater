"""Mode policy configuration and contracts (Phase P1 / spec §4, §10.1)."""

from __future__ import annotations

import importlib
from typing import Any

from trading.domain.contracts.base import ExactDecimal, NonEmptyStr, StrictModel
from trading.domain.enums import FamilyId, ModeId

__all__ = [
    "ModePolicy",
    "ModesConfig",
    "load_modes_config",
]

# Type alias avoiding infrastructure import in domain layer (test_import_boundaries).
Path = Any


class ModePolicy(StrictModel):
    """Execution policy for one of the four trading modes."""

    mode_id: ModeId
    mandate: NonEmptyStr
    capital_share: ExactDecimal
    per_trade_loss_cap_fraction: ExactDecimal
    max_open_loss_cap_fraction: ExactDecimal
    daily_budget_cap_fraction: ExactDecimal
    allowed_families: tuple[FamilyId, ...]
    holding_style: NonEmptyStr
    window_start_ist: NonEmptyStr
    window_end_ist: NonEmptyStr
    expiry_rule: NonEmptyStr
    review_cadence: tuple[NonEmptyStr, ...]


class ModesConfig(StrictModel):
    """Complete four-mode configuration mapping."""

    schema_version: NonEmptyStr = "1"
    modes: dict[ModeId, ModePolicy]


def load_modes_config(path: Path | None = None) -> ModesConfig:
    """Load and validate modes policy configuration from YAML."""
    path_cls = importlib.import_module("pathlib").Path
    yaml_mod = importlib.import_module("yaml")

    if path is None:
        repo_config = path_cls(__file__).resolve().parents[4] / "config" / "modes.yaml"
        target_path = (
            repo_config if repo_config.is_file() else path_cls("config/modes.yaml")
        )
    else:
        target_path = path_cls(path)

    raw: Any = yaml_mod.safe_load(target_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"expected mapping in {target_path}, got {type(raw).__name__}")
    return ModesConfig.model_validate(raw)
