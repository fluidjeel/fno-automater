"""ADESK-A8: reason preconditions downgrade ungrounded actions to ABSTAIN."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from trading.analytics.reason_preconditions import EvidenceBundle, ground_agent_reasons
from trading.domain.clock import FrozenClock
from trading.domain.enums import AgentAction, AgentReasonCode, DeskRole
from trading.storage.trading_store import TradingStore

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


def test_grounded_reasons_keep_action() -> None:
    evidence = EvidenceBundle(
        iv_percentile_delta=Decimal("15"),
        trend_state="UP",
        thesis_direction="UP",
        spread_bps=Decimal("10"),
        structure_is_defined_risk=True,
    )
    result, event = ground_agent_reasons(
        reason_codes=(
            AgentReasonCode.VOLATILITY_EXPANSION,
            AgentReasonCode.REGIME_ALIGNMENT,
            AgentReasonCode.LIQUIDITY_ADEQUATE,
            AgentReasonCode.STRUCTURE_DEFINED_RISK,
        ),
        evidence=evidence,
        action=AgentAction.VETO_ENTRY,
        decision_id="d1",
        role=DeskRole.ENTRY,
        model_id="m1",
        prompt_version="p1",
        event_id="h1",
        created_at=NOW,
    )
    assert result.downgraded is False
    assert result.effective_action is AgentAction.VETO_ENTRY
    assert event is None
    assert AgentReasonCode.VOLATILITY_EXPANSION in result.grounded_codes


def test_ungrounded_reason_downgrades_to_abstain() -> None:
    evidence = EvidenceBundle(iv_percentile_delta=Decimal("1"))  # not expansion
    result, event = ground_agent_reasons(
        reason_codes=(AgentReasonCode.VOLATILITY_EXPANSION,),
        evidence=evidence,
        action=AgentAction.VETO_ENTRY,
        decision_id="d2",
        role=DeskRole.ENTRY,
        model_id="m1",
        prompt_version="p1",
        event_id="h2",
        created_at=NOW,
    )
    assert result.downgraded is True
    assert result.effective_action is AgentAction.ABSTAIN
    assert result.ungrounded_codes == (AgentReasonCode.VOLATILITY_EXPANSION,)
    assert event is not None
    assert event.effective_action is AgentAction.ABSTAIN


def test_hallucination_event_persists(tmp_path: Path) -> None:
    evidence = EvidenceBundle(event_blackout=True)
    result, event = ground_agent_reasons(
        reason_codes=(AgentReasonCode.EVENT_CLEAR,),
        evidence=evidence,
        action=AgentAction.HOLD,
        decision_id="d3",
        role=DeskRole.MACRO,
        model_id="m1",
        prompt_version="p1",
        event_id="h3",
        created_at=NOW,
    )
    assert result.downgraded and event is not None
    store = TradingStore.open(tmp_path / "t.sqlite", clock=FrozenClock(NOW))
    try:
        store.insert_hallucination_event(event)
        listed = store.list_hallucination_events(decision_id="d3")
        assert len(listed) == 1
        assert listed[0].event_id == "h3"
    finally:
        store.close()
