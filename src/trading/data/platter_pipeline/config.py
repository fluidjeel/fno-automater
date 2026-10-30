"""Load validated settings for the platter data pipeline."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = ["PlatterPipelineConfig", "load_platter_pipeline_config"]


class _ScheduleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    minute_backbone_ist: str
    platter_grid_ist: str
    agent_lag_seconds_max: int = Field(ge=1, le=120)


class _SymbolsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    nifty_index: str
    vix_index: str
    chain_strike_count: int = Field(ge=1, le=50)


class _ThresholdsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    large_print_contracts: int = Field(ge=1)
    absorption_min_aggressive_volume: int = Field(ge=1)
    absorption_max_progress_ticks: int = Field(ge=0)
    opening_range_minutes: tuple[int, ...]


class _RubricWeights(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    structure: str
    flow: str
    depth: str
    vol: str
    options: str
    events: str

    @field_validator(
        "structure",
        "flow",
        "depth",
        "vol",
        "options",
        "events",
    )
    @classmethod
    def _dec(cls, value: str) -> str:
        Decimal(value)
        return value


class _OptionsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    gex_formula_version: str
    lot_size: int = Field(ge=1)


class _AgentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    llm_enabled: bool
    model_id: str
    thinking: Literal["disabled"] = "disabled"


class _PaperModeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    free_trade: bool = True


class _TelemetryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    retention_days: int = Field(ge=7, le=730)
    compact_ticks_after_days: int = Field(ge=1, le=365)
    keep_weekly_rollups_days: int = Field(ge=30, le=730)
    min_free_gb: int = Field(ge=1, le=100)


class _ShadowModeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool
    gate_authority: Literal["legacy", "platter"]
    log_path: str


class PlatterPipelineConfig(BaseModel):
    """Validated platter revamp settings from YAML."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str
    calculation_version: str
    schedule: _ScheduleConfig
    symbols: _SymbolsConfig
    thresholds: _ThresholdsConfig
    rubric_weights: _RubricWeights
    rubric_direction_cutoff: str
    options: _OptionsConfig
    agent: _AgentConfig
    paper_mode: _PaperModeConfig
    telemetry: _TelemetryConfig
    shadow_mode: _ShadowModeConfig

    @field_validator("rubric_direction_cutoff")
    @classmethod
    def _cutoff(cls, value: str) -> str:
        Decimal(value)
        return value

    @field_validator("rubric_weights")
    @classmethod
    def _weights_sum(cls, value: _RubricWeights) -> _RubricWeights:
        total = sum(
            Decimal(getattr(value, key))
            for key in ("structure", "flow", "depth", "vol", "options", "events")
        )
        if total != Decimal("1"):
            raise ValueError("rubric_weights must sum to 1")
        return value


def load_platter_pipeline_config(
    path: Path | None = None,
) -> PlatterPipelineConfig:
    """Load config from repo `config/platter_pipeline.yaml`."""
    if path is None:
        root = Path(__file__).resolve().parents[4]
        target = root / "config" / "platter_pipeline.yaml"
    else:
        target = path
    raw = yaml.safe_load(target.read_text(encoding="utf-8"))
    return PlatterPipelineConfig.model_validate(raw)
