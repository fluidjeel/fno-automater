"""Load the versioned PAPER two-tier market-data contract."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from trading.domain.contracts.paper_data import PaperDataRequirements

__all__ = ["PaperDataConfigError", "load_paper_data_requirements"]


class PaperDataConfigError(ValueError):
    """Raised when the paper-data contract cannot be parsed or is incomplete."""


def load_paper_data_requirements(path: Path) -> PaperDataRequirements:
    """Parse and validate the closed P0/P1 field matrix."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PaperDataConfigError(f"cannot read {path}: {exc}") from exc
    try:
        payload: Any = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise PaperDataConfigError(f"{path}: not valid YAML: {exc}") from exc
    if not isinstance(payload, dict):
        raise PaperDataConfigError(f"{path}: expected a mapping at the top level")
    payload.pop("schema_version", None)
    try:
        return PaperDataRequirements.model_validate(payload)
    except (ValueError, TypeError) as exc:
        raise PaperDataConfigError(f"{path}: {exc}") from exc
