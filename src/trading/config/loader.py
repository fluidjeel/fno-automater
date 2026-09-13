"""Configuration loading with a content checksum for decision lineage.

Invariant 21: the same snapshot, config and code version must reproduce the same
decision, so a decision has to name the exact configuration it saw. The checksum
is taken over the file bytes, not the parsed model, so a comment-only edit is
visible too: if a human touched the file, the version changes and the change is
attributable.

Invariant 23: a live config change needs a version and checksum, which is what
LoadedConfig carries into every audit record.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from trading.config.schema import AppConfig, Environment

__all__ = ["ConfigLoadError", "LoadedConfig", "load_config", "load_config_text"]


class ConfigLoadError(ValueError):
    """Raised when configuration cannot be parsed or fails validation."""


@dataclass(frozen=True, slots=True)
class LoadedConfig:
    """A validated configuration plus the identity of the bytes it came from."""

    config: AppConfig
    version: str
    checksum: str
    source: str

    @property
    def lineage(self) -> tuple[str, str]:
        """The pair every decision record must carry."""
        return self.version, self.checksum


def _checksum(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def load_config_text(
    raw: str,
    *,
    source: str = "<string>",
    expect_environment: Environment | None = None,
) -> LoadedConfig:
    """Parse, validate and checksum configuration from text."""
    try:
        parsed: Any = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ConfigLoadError(f"{source}: not valid YAML: {exc}") from exc

    if not isinstance(parsed, dict):
        raise ConfigLoadError(
            f"{source}: expected a mapping at the top level, got "
            f"{type(parsed).__name__}"
        )

    config = AppConfig.model_validate(parsed)

    if expect_environment is not None and config.environment is not expect_environment:
        raise ConfigLoadError(
            f"{source}: configuration declares {config.environment} but "
            f"{expect_environment} was expected. Loading the wrong environment is "
            "how paper credentials reach a live account, so this is fatal."
        )

    # Fails closed when a live account would read an unverified market rule.
    config.require_ready_for(config.environment)

    return LoadedConfig(
        config=config,
        version=config.schema_version,
        checksum=_checksum(raw.encode("utf-8")),
        source=source,
    )


def load_config(
    path: Path,
    *,
    expect_environment: Environment | None = None,
) -> LoadedConfig:
    """Load configuration from a file."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigLoadError(f"cannot read {path}: {exc}") from exc
    return load_config_text(
        raw, source=str(path), expect_environment=expect_environment
    )
