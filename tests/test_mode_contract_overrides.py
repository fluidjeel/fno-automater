"""Per-mode contract overrides keep M3 and M4 binder parameters independent."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from trading.domain.enums import ModeId
from trading.identification.config import (
    ContractOverrides,
    load_identification_policy,
)

REPO = Path(__file__).resolve().parents[1]


def test_m3_dte_floor_matches_strategy_floor() -> None:
    """The binder no longer offers M3 pairs the strategy's 7-DTE floor rejects."""
    policy = load_identification_policy(REPO / "config" / "identification.yaml")
    m3 = policy.for_mode(ModeId.M3_TACTICAL_POSITIONAL)
    assert m3.contracts.weekly_dte_min == 7
    assert m3.contracts.weekly_dte_max == policy.contracts.weekly_dte_max
    assert policy.contracts.weekly_dte_min == 5


def test_modes_without_overrides_share_the_base_policy() -> None:
    policy = load_identification_policy(REPO / "config" / "identification.yaml")
    assert policy.for_mode(ModeId.M2_DIRECTIONAL) is policy
    assert policy.for_mode(ModeId.M4_STRATEGIC_POSITIONAL).contracts == policy.contracts


def test_invalid_override_fails_closed() -> None:
    """An out-of-range override is rejected at load, not at bind time."""
    with pytest.raises(ValidationError):
        ContractOverrides.model_validate({"max_spread_fraction": "1.5"})
