"""ADESK-D6: Phase-2 upscale — deterministic envelope only; never agent BOUNDED."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from trading.domain.contracts.agent_decision import AgentDecision
from trading.domain.contracts.authority import AuthorityGrant
from trading.domain.contracts.phase2_sizing import (
    PHASE2_HARD_CAP,
    PHASE2_M_CEILING_DEFAULT,
    CalibrationGate,
    Phase2UpscaleEnvelope,
    agent_size_multiplier_rejected_above_one,
    apply_deterministic_upscale,
)
from trading.domain.enums import (
    AgentAction,
    AuthorityMode,
    ConfidenceBucket,
    DeskRole,
    Environment,
    GateOutcome,
)

NOW = datetime(2026, 9, 20, 16, 0, tzinfo=UTC)


def _monotonic_accuracies() -> tuple[Decimal, ...]:
    # One per ConfidenceBucket in enum order.
    assert list(ConfidenceBucket) == [
        ConfidenceBucket.P10,
        ConfidenceBucket.P30,
        ConfidenceBucket.P50,
        ConfidenceBucket.P70,
        ConfidenceBucket.P90,
    ]
    return (
        Decimal("0.20"),
        Decimal("0.35"),
        Decimal("0.50"),
        Decimal("0.65"),
        Decimal("0.80"),
    )


def _unlocked_gate(**overrides: object) -> CalibrationGate:
    payload: dict[str, object] = {
        "scored_decisions": 150,
        "brier_score": Decimal("0.10"),
        "reliability_component": Decimal("0.02"),
        "bucket_accuracies": _monotonic_accuracies(),
    }
    payload.update(overrides)
    return CalibrationGate.model_validate(payload)


def test_locked_when_sample_insufficient() -> None:
    gate = _unlocked_gate(scored_decisions=149)
    assert not gate.unlocked
    env = Phase2UpscaleEnvelope(gate=gate)
    assert apply_deterministic_upscale(Decimal("1.25"), envelope=env) == Decimal("1")


def test_locked_when_buckets_non_monotonic() -> None:
    bad = (
        Decimal("0.20"),
        Decimal("0.40"),
        Decimal("0.35"),  # dip
        Decimal("0.60"),
        Decimal("0.80"),
    )
    gate = _unlocked_gate(bucket_accuracies=bad)
    assert not gate.monotonic_buckets
    assert not gate.unlocked


def test_calibrated_upscale_via_deterministic_path() -> None:
    gate = _unlocked_gate()
    assert gate.unlocked
    env = Phase2UpscaleEnvelope(gate=gate, m_ceiling=PHASE2_M_CEILING_DEFAULT)
    assert env.unlocked
    assert apply_deterministic_upscale(Decimal("1.25"), envelope=env) == Decimal(
        "1.25"
    )
    assert apply_deterministic_upscale(Decimal("1.40"), envelope=env) == Decimal(
        "1.25"
    )


def test_hard_cap_enforced_on_envelope() -> None:
    with pytest.raises(ValidationError):
        Phase2UpscaleEnvelope(
            gate=_unlocked_gate(),
            m_ceiling=Decimal("1.51"),
        )
    assert Decimal("1.50") == PHASE2_HARD_CAP


def test_agent_path_rejects_multiplier_above_one() -> None:
    assert agent_size_multiplier_rejected_above_one(Decimal("1.01"))
    assert not agent_size_multiplier_rejected_above_one(Decimal("1.00"))
    with pytest.raises(ValidationError):
        AgentDecision(
            decision_id="DEC-1",
            run_id="RUN-1",
            role=DeskRole.ENTRY,
            mode=AuthorityMode.SHADOW,
            environment=Environment.PAPER,
            snapshot_id="S1",
            action=AgentAction.ABSTAIN,
            size_multiplier=Decimal("1.25"),
            agent_override=False,
            reason_codes=("X",),
            ungrounded_codes=(),
            evidence_ids=(),
            gate_outcome=GateOutcome.SHADOW_ONLY,
            model_id="m",
            prompt_version="p",
            policy_version="pol",
            packet_version="pkt",
            input_tokens=0,
            output_tokens=0,
            latency_ms=0,
            created_at=NOW,
        )


def test_agent_bounded_upscale_action_rejected_at_grant() -> None:
    """PROPOSE_SIZE_INCREASE is live-path; cannot appear on a BOUNDED grant."""
    with pytest.raises(ValidationError, match="live-path"):
        AuthorityGrant.issue(
            grant_id="G-UP",
            role=DeskRole.ENTRY,
            mode=AuthorityMode.BOUNDED,
            allowed_actions=(AgentAction.PROPOSE_SIZE_INCREASE,),
            strategy_families=("positional_long_option",),
            environment=Environment.PAPER,
            policy_version="p",
            prompt_version="pr",
            model_id="m",
            evidence_report_id="e",
            granted_at=NOW - timedelta(hours=1),
            valid_until=NOW + timedelta(days=7),
            signed_by="op",
        )
