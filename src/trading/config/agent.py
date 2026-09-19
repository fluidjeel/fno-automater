"""Versioned Layer 4 agent budget and model policy."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field

from trading.config.loader import ConfigLoadError
from trading.domain.contracts.base import (
    ExactDecimal,
    NonEmptyStr,
    StrictInt,
    VersionedModel,
)

__all__ = [
    "AgentConfig",
    "AgentConfigError",
    "LoadedAgentConfig",
    "load_agent_config",
    "load_agent_config_text",
]


class AgentConfigError(ConfigLoadError):
    """Raised when agent policy cannot be parsed."""


class AgentConfig(VersionedModel):
    """Bounded weekly-agent settings. Fail closed when disabled or over budget."""

    policy_version: NonEmptyStr
    enabled: bool
    model: NonEmptyStr
    max_iterations: StrictInt = Field(ge=1, le=32)
    max_tokens_per_run: StrictInt = Field(gt=0)
    monthly_budget_inr: ExactDecimal = Field(ge=0)
    input_inr_per_million_tokens: ExactDecimal = Field(ge=0)
    output_inr_per_million_tokens: ExactDecimal = Field(ge=0)


@dataclass(frozen=True, slots=True)
class LoadedAgentConfig:
    """Agent policy plus the identity of the bytes it came from."""

    config: AgentConfig
    version: str
    checksum: str
    source: str


def load_agent_config(path: Path) -> LoadedAgentConfig:
    """Load agent policy from a file."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise AgentConfigError(f"cannot read {path}: {exc}") from exc
    return load_agent_config_text(raw, source=str(path))


def load_agent_config_text(
    raw: str,
    *,
    source: str = "<string>",
) -> LoadedAgentConfig:
    """Parse and checksum agent policy from text."""
    try:
        parsed: Any = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise AgentConfigError(f"{source}: not valid YAML: {exc}") from exc
    if not isinstance(parsed, dict):
        raise AgentConfigError(
            f"{source}: expected a mapping at the top level, got "
            f"{type(parsed).__name__}"
        )
    config = AgentConfig.model_validate(parsed)
    return LoadedAgentConfig(
        config=config,
        version=config.schema_version,
        checksum=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        source=source,
    )
