"""DISCOVERY vs STRICT evidence profile helpers for Layer 4 analytics."""

from __future__ import annotations

from collections.abc import Sequence

from trading.config.discovery import DISCOVERY_EXPERIMENT_PREFIX
from trading.domain.contracts.evaluation import CohortPackage

DISCOVERY_INELIGIBLE_REASON = "discovery cohorts are not promotion evidence"

__all__ = [
    "DISCOVERY_INELIGIBLE_REASON",
    "is_discovery_experiment_id",
    "refuse_mixed_evidence_profiles",
]


def is_discovery_experiment_id(experiment_id: str) -> bool:
    """Return True when the cohort id belongs to a DISCOVERY evidence package."""
    return experiment_id.startswith(f"{DISCOVERY_EXPERIMENT_PREFIX}-")


def refuse_mixed_evidence_profiles(packages: Sequence[CohortPackage]) -> None:
    """Refuse to score DISCOVERY and STRICT cohorts as one evidence package."""
    profiles = {
        "DISCOVERY"
        if is_discovery_experiment_id(package.experiment.experiment_id)
        else "STRICT"
        for package in packages
    }
    if len(profiles) > 1:
        raise ValueError("do not mix DISCOVERY and STRICT cohorts")
