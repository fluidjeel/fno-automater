"""M2 A/B study configuration (control vs pre-close early-exit shadow)."""

from __future__ import annotations

import hashlib
import importlib
from dataclasses import dataclass
from datetime import time
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import Field

from trading.domain.contracts.base import ExactDecimal, NonEmptyStr, StrictModel

__all__ = ["LoadedM2AbStudyConfig", "M2AbStudyConfig", "load_m2_ab_study_config"]


class M2EarlyExitConfig(StrictModel):
    """Pre-close early exit window for Mode B shadow."""

    window_start_local: NonEmptyStr = "14:30"
    window_end_local: NonEmptyStr = "15:00"
    min_loss_fraction_of_max_loss: ExactDecimal = Field(
        default=Decimal("0.75"),
        description="Exit when loss >= this fraction of Layer 2 max loss.",
    )


class M2AbStudyConfig(StrictModel):
    """Parallel M2 paper study: Mode A live, Mode B shadow."""

    schema_version: NonEmptyStr = "1"
    enabled: bool = False
    results_dir: NonEmptyStr = "data/paper/m2_ab"
    mode_a_results_file: NonEmptyStr = "results_mode_a.jsonl"
    mode_b_results_file: NonEmptyStr = "results_mode_b.jsonl"
    comparison_file: NonEmptyStr = "comparison_events.jsonl"
    early_exit: M2EarlyExitConfig = Field(default_factory=M2EarlyExitConfig)


@dataclass(frozen=True, slots=True)
class LoadedM2AbStudyConfig:
    """Validated study config plus checksum."""

    config: M2AbStudyConfig
    checksum: str
    source: str


def _parse_hhmm(value: str) -> time:
    hour, minute = (int(part) for part in value.split(":", 1))
    return time(hour, minute)


def early_exit_window(config: M2AbStudyConfig) -> tuple[time, time]:
    """IST local window for Mode B pre-close early exit."""
    return (
        _parse_hhmm(config.early_exit.window_start_local),
        _parse_hhmm(config.early_exit.window_end_local),
    )


def load_m2_ab_study_config(path: Path | None = None) -> LoadedM2AbStudyConfig:
    """Load study YAML; missing file yields disabled config."""
    path_cls = importlib.import_module("pathlib").Path
    yaml_mod = importlib.import_module("yaml")
    if path is None:
        repo = path_cls(__file__).resolve().parents[3] / "config" / "m2_ab_study.yaml"
        target = repo if repo.is_file() else path_cls("config/m2_ab_study.yaml")
    else:
        target = path_cls(path)
    if not target.is_file():
        disabled = M2AbStudyConfig(enabled=False)
        return LoadedM2AbStudyConfig(
            config=disabled, checksum="disabled", source=str(target)
        )
    raw_bytes = target.read_bytes()
    raw: Any = yaml_mod.safe_load(raw_bytes.decode("utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"expected mapping in {target}")
    cfg = M2AbStudyConfig.model_validate(raw)
    digest = hashlib.sha256(raw_bytes).hexdigest()
    return LoadedM2AbStudyConfig(config=cfg, checksum=digest, source=str(target))
