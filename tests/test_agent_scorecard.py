"""ADESK-B10: agent_scorecard PART 14 metrics + CLI smoke."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from trading.analytics.agent_scorecard import (
    abstention_rate,
    build_agent_scorecard,
    hallucination_rate,
    l0_resolution_rate,
    override_rate,
)
from trading.domain.contracts.agent_decision import AgentDecision
from trading.domain.enums import (
    AgentAction,
    AuthorityMode,
    DeskRole,
    Environment,
    GateOutcome,
)

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


def _dec(**overrides: object) -> AgentDecision:
    payload: dict[str, object] = {
        "decision_id": "d1",
        "run_id": "r1",
        "role": DeskRole.ENTRY,
        "mode": AuthorityMode.SHADOW,
        "environment": Environment.PAPER,
        "snapshot_id": "s",
        "action": AgentAction.SELECT_STRIKE_CANDIDATE,
        "agent_override": False,
        "reason_codes": ("OK",),
        "ungrounded_codes": (),
        "evidence_ids": (),
        "gate_outcome": GateOutcome.SHADOW_ONLY,
        "model_id": "m",
        "prompt_version": "p",
        "policy_version": "pol",
        "packet_version": "pkt",
        "input_tokens": 10,
        "output_tokens": 5,
        "latency_ms": 100,
        "created_at": NOW,
    }
    payload.update(overrides)
    return AgentDecision.model_validate(payload)


def test_universal_metrics_on_empty_are_zero() -> None:
    assert l0_resolution_rate(()) == Decimal("0")
    assert hallucination_rate(()) == Decimal("0")
    assert abstention_rate(()) == Decimal("0")
    assert override_rate(()) == Decimal("0")


def test_build_scorecard_entry_stubs() -> None:
    decisions = (
        _dec(decision_id="a"),
        _dec(
            decision_id="b",
            action=AgentAction.ABSTAIN,
            agent_override=True,
            ungrounded_codes=("X",),
        ),
    )
    card = build_agent_scorecard(decisions, role=DeskRole.ENTRY, as_of=NOW)
    assert card.decisions == 2
    assert card.abstention_rate == Decimal("0.5000")
    assert card.override_rate == Decimal("0.5000")
    assert card.hallucination_rate == Decimal("0.5000")
    assert card.entry_precision == Decimal("0")
    assert card.review_precision is None
