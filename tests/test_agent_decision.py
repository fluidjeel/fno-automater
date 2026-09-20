"""ADESK-A2: AgentDecision contract, DecisionLog writer, agent_decisions store.

Invariant 2: a logged decision cannot place an order or mutate live config.
Zero LLM calls. C1 BOUNDED rules are unchanged; this slice only persists audit.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from trading.ai.decision_log import DecisionLog
from trading.domain.clock import FrozenClock
from trading.domain.contracts.agent_decision import AgentDecision
from trading.domain.enums import (
    AgentAction,
    AuthorityMode,
    DeskRole,
    Environment,
    GateOutcome,
)
from trading.storage.trading_store import DuplicateAgentDecisionError, TradingStore

NOW = datetime(2026, 9, 20, 6, 0, tzinfo=UTC)
MODEL = "deepseek-chat-20250301"
PROMPT = "desk-prompt-v1"
POLICY = "desk-policy-v1"
PACKET = "entry-packet-v1"


def _decision_kwargs(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "decision_id": "DEC-ENTRY-1",
        "run_id": "RUN-1",
        "role": DeskRole.ENTRY,
        "mode": AuthorityMode.SHADOW,
        "environment": Environment.PAPER,
        "trade_id": "TRD-1",
        "snapshot_id": "SNAP-1",
        "action": AgentAction.SELECT_STRIKE_CANDIDATE,
        "confidence": Decimal("0.80"),
        "size_multiplier": Decimal("1.00"),
        "deterministic_choice": "CAND-RANK-1",
        "agent_override": True,
        "reason_codes": ("IV_SKEW_FAVOURABLE", "SPREAD_OK"),
        "ungrounded_codes": ("UNGROUNDED_MACRO_CLAIM",),
        "evidence_ids": ("EV-1", "EV-2"),
        "gate_outcome": GateOutcome.SHADOW_ONLY,
        "gate_reject_codes": None,
        "model_id": MODEL,
        "prompt_version": PROMPT,
        "policy_version": POLICY,
        "packet_version": PACKET,
        "input_tokens": 120,
        "output_tokens": 40,
        "latency_ms": 250,
        "created_at": NOW,
    }
    payload.update(overrides)
    return payload


def _decision(**overrides: object) -> AgentDecision:
    return AgentDecision.model_validate(_decision_kwargs(**overrides))


@pytest.fixture
def store(tmp_path: Path) -> Iterator[TradingStore]:
    trading_store = TradingStore.open(
        tmp_path / "decisions.sqlite", clock=FrozenClock(NOW)
    )
    yield trading_store
    trading_store.close()


@pytest.fixture
def log(store: TradingStore) -> DecisionLog:
    return DecisionLog(store)


class TestAgentDecisionContract:
    def test_float_confidence_is_rejected(self) -> None:
        """Invariant 4: float never reaches an accounting or audit numeric field."""
        with pytest.raises(ValidationError):
            _decision(confidence=0.8)

    def test_negative_tokens_are_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _decision(input_tokens=-1)

    def test_json_round_trip_preserves_tuples(self) -> None:
        decision = _decision()
        restored = decision.round_trip()
        assert restored == decision
        assert restored.reason_codes == ("IV_SKEW_FAVOURABLE", "SPREAD_OK")
        assert restored.ungrounded_codes == ("UNGROUNDED_MACRO_CLAIM",)
        assert restored.evidence_ids == ("EV-1", "EV-2")


class TestDecisionLogRoundTrip:
    def test_record_then_get_equals_original(self, log: DecisionLog) -> None:
        decision = _decision()
        recorded = log.record(decision)
        loaded = log.get(decision.decision_id)
        assert recorded == decision
        assert loaded == decision
        assert loaded is not None
        assert loaded.reason_codes == ("IV_SKEW_FAVOURABLE", "SPREAD_OK")
        assert loaded.ungrounded_codes == ("UNGROUNDED_MACRO_CLAIM",)
        assert loaded.evidence_ids == ("EV-1", "EV-2")
        assert loaded.gate_reject_codes is None
        assert loaded.confidence == Decimal("0.80")
        assert loaded.size_multiplier == Decimal("1.00")
        assert loaded.agent_override is True

    def test_optional_fields_survive_as_none(self, log: DecisionLog) -> None:
        decision = _decision(
            trade_id=None,
            confidence=None,
            size_multiplier=None,
            deterministic_choice=None,
            agent_override=False,
            gate_reject_codes=None,
        )
        log.record(decision)
        loaded = log.get(decision.decision_id)
        assert loaded == decision
        assert loaded is not None
        assert loaded.trade_id is None
        assert loaded.confidence is None
        assert loaded.size_multiplier is None
        assert loaded.deterministic_choice is None

    def test_gate_reject_codes_tuple_survives(self, log: DecisionLog) -> None:
        decision = _decision(
            action=AgentAction.VETO_ENTRY,
            gate_outcome=GateOutcome.REJECTED,
            gate_reject_codes=("MODE_OBSERVE", "LIVE_PATH_FORBIDDEN"),
            agent_override=False,
        )
        log.record(decision)
        loaded = log.get(decision.decision_id)
        assert loaded is not None
        assert loaded.gate_reject_codes == ("MODE_OBSERVE", "LIVE_PATH_FORBIDDEN")
        assert loaded.gate_outcome is GateOutcome.REJECTED

    def test_missing_id_returns_none(self, log: DecisionLog) -> None:
        assert log.get("DEC-MISSING") is None


class TestDecisionLogQueries:
    def test_query_by_role(self, log: DecisionLog) -> None:
        entry = _decision(decision_id="DEC-ENTRY-1", role=DeskRole.ENTRY)
        portfolio = _decision(
            decision_id="DEC-PORTFOLIO-1",
            role=DeskRole.PORTFOLIO,
            action=AgentAction.PROPOSE_FAMILY_HALT,
            created_at=NOW + timedelta(seconds=1),
        )
        log.record(entry)
        log.record(portfolio)
        assert log.list(role=DeskRole.ENTRY) == (entry,)
        assert log.list(role=DeskRole.PORTFOLIO) == (portfolio,)
        assert log.list(role=DeskRole.MACRO) == ()
        assert log.list() == (entry, portfolio)

    def test_query_by_version_triple(self, log: DecisionLog) -> None:
        v1 = _decision(decision_id="DEC-V1")
        v2 = _decision(
            decision_id="DEC-V2",
            model_id="other-model",
            prompt_version="desk-prompt-v2",
            policy_version="desk-policy-v2",
            created_at=NOW + timedelta(seconds=1),
        )
        log.record(v1)
        log.record(v2)
        matched = log.list(model_id=MODEL, prompt_version=PROMPT, policy_version=POLICY)
        assert matched == (v1,)
        other = log.list(
            model_id="other-model",
            prompt_version="desk-prompt-v2",
            policy_version="desk-policy-v2",
        )
        assert other == (v2,)

    def test_query_by_role_and_version_triple(self, log: DecisionLog) -> None:
        keep = _decision(decision_id="DEC-KEEP", role=DeskRole.ENTRY)
        other_role = _decision(
            decision_id="DEC-OTHER-ROLE",
            role=DeskRole.POSITION,
            action=AgentAction.HOLD,
        )
        other_version = _decision(
            decision_id="DEC-OTHER-VER",
            role=DeskRole.ENTRY,
            prompt_version="desk-prompt-v2",
        )
        log.record(keep)
        log.record(other_role)
        log.record(other_version)
        assert log.list(
            role=DeskRole.ENTRY,
            model_id=MODEL,
            prompt_version=PROMPT,
            policy_version=POLICY,
        ) == (keep,)

    def test_partial_version_triple_is_rejected(self, log: DecisionLog) -> None:
        with pytest.raises(ValueError, match="version triple"):
            log.list(model_id=MODEL)


class TestDuplicateDecisionId:
    def test_duplicate_decision_id_fails_closed(self, log: DecisionLog) -> None:
        """A retry must not rewrite the append-only audit row."""
        first = _decision()
        log.record(first)
        with pytest.raises(DuplicateAgentDecisionError, match="DEC-ENTRY-1"):
            log.record(
                _decision(
                    reason_codes=("DIFFERENT",),
                    agent_override=False,
                )
            )
        loaded = log.get("DEC-ENTRY-1")
        assert loaded == first
        assert loaded is not None
        assert loaded.reason_codes == ("IV_SKEW_FAVOURABLE", "SPREAD_OK")

    def test_store_insert_rejects_duplicate(self, store: TradingStore) -> None:
        decision = _decision()
        store.insert_agent_decision(decision)
        with pytest.raises(DuplicateAgentDecisionError):
            store.insert_agent_decision(decision)
        assert store.get_agent_decision(decision.decision_id) == decision
