"""ADESK-B2: ENTRY desk StrikeShortlist / EntryAdvice SHADOW.

Proves: shadow logged; live path unchanged when disabled; validator rejects
strike not on shortlist. Zero OMS/broker influence.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from tests import factories as f
from trading.ai.decision_log import DecisionLog
from trading.ai.entry import (
    build_shadow_entry_advice,
    maybe_log_entry_shadow,
)
from trading.domain.clock import FrozenClock
from trading.domain.contracts.advice import StructureChoice
from trading.domain.contracts.entry import (
    EntryAdvice,
    EntryAdviceError,
    LegSpec,
    StrikeCandidate,
    StrikeShortlist,
    assert_candidate_on_shortlist,
)
from trading.domain.enums import (
    AgentAction,
    AuthorityMode,
    DeskRole,
    EntryGateId,
    GateOutcome,
    LiquidityGrade,
    Side,
    VetoCode,
)
from trading.domain.primitives import Currency, Money, Percent
from trading.storage.trading_store import TradingStore

NOW = datetime(2026, 9, 20, 10, 0, tzinfo=UTC)
INR = Currency.INR


def _money(v: str) -> Money:
    return Money.of(v, INR)


def _leg(leg_id: str = "L1") -> LegSpec:
    return LegSpec(
        leg_id=leg_id,
        contract=f.option_contract(),
        side=Side.BUY,
        ratio=1,
    )


def _candidate(
    candidate_id: str,
    *,
    score: str = "0.80",
    grade: LiquidityGrade = LiquidityGrade.A,
) -> StrikeCandidate:
    return StrikeCandidate(
        candidate_id=candidate_id,
        legs=(_leg(candidate_id),),
        net_debit=_money("5000"),
        max_loss=_money("5000"),
        delta=Decimal("0.45"),
        vega=Decimal("12"),
        theta_per_day=Decimal("-25"),
        iv=Decimal("0.15"),
        iv_percentile=Decimal("40"),
        bid_ask_spread_pct=Percent.from_percent("1"),
        observed_depth_lots=10,
        breakeven_move_pct=Percent.from_percent("1.5"),
        deterministic_score=Decimal(score),
        liquidity_grade=grade,
    )


def _shortlist(*ids_scores: tuple[str, str]) -> StrikeShortlist:
    if not ids_scores:
        ids_scores = (("C1", "0.90"), ("C2", "0.70"))
    return StrikeShortlist(
        snapshot_id="SNAP-1",
        underlying="NIFTY",
        expiry=date(2026, 9, 25),
        structure=StructureChoice.DEBIT_SPREAD,
        candidates=tuple(_candidate(i, score=s) for i, s in ids_scores),
        shortlist_rule_version="shortlist-v1",
    )


def test_shortlist_eligible_requires_two_candidates() -> None:
    one = _shortlist(("C1", "0.9"))
    assert one.eligible_for_agent is False
    two = _shortlist(("C1", "0.9"), ("C2", "0.5"))
    assert two.eligible_for_agent is True


def test_duplicate_candidate_ids_rejected() -> None:
    with pytest.raises(ValidationError):
        StrikeShortlist(
            snapshot_id="SNAP-1",
            underlying="NIFTY",
            expiry=date(2026, 9, 25),
            structure=StructureChoice.PASS,
            candidates=(_candidate("C1"), _candidate("C1", score="0.1")),
            shortlist_rule_version="v1",
        )


def test_entry_advice_select_requires_thesis_and_candidate() -> None:
    with pytest.raises(ValidationError):
        EntryAdvice(
            as_of=NOW,
            snapshot_id="SNAP-1",
            action=AgentAction.SELECT_STRIKE_CANDIDATE,
            candidate_id=None,
            size_multiplier=Decimal("1"),
            thesis=None,
            narrative="missing",
        )


def test_veto_requires_codes() -> None:
    with pytest.raises(ValidationError):
        EntryAdvice(
            as_of=NOW,
            snapshot_id="SNAP-1",
            action=AgentAction.VETO_ENTRY,
            size_multiplier=Decimal("1"),
            veto_codes=(),
            narrative="veto without codes",
        )
    ok = EntryAdvice(
        as_of=NOW,
        snapshot_id="SNAP-1",
        action=AgentAction.VETO_ENTRY,
        size_multiplier=Decimal("0.5"),
        veto_codes=(VetoCode.SPREAD_WIDE,),
        failed_gate_ids=(EntryGateId.ACTION_NOT_ALLOWED,),
        narrative="spread too wide",
    )
    assert ok.action is AgentAction.VETO_ENTRY


def test_validator_rejects_strike_not_on_shortlist() -> None:
    shortlist = _shortlist(("C1", "0.9"), ("C2", "0.5"))
    advice = build_shadow_entry_advice(shortlist, as_of=NOW)
    assert advice is not None
    bad = advice.model_copy(update={"candidate_id": "NOT-ON-LIST"})
    with pytest.raises(EntryAdviceError):
        assert_candidate_on_shortlist(bad, shortlist)


def test_shadow_picks_deterministic_top() -> None:
    shortlist = _shortlist(("LOW", "0.40"), ("HIGH", "0.95"), ("MID", "0.70"))
    advice = build_shadow_entry_advice(shortlist, as_of=NOW, trade_id="T1")
    assert advice is not None
    assert advice.candidate_id == "HIGH"
    assert advice.agent_override is False
    assert advice.thesis is not None
    assert_candidate_on_shortlist(advice, shortlist)


def test_skip_agent_when_shortlist_too_small() -> None:
    shortlist = _shortlist(("ONLY", "1.0"))
    assert build_shadow_entry_advice(shortlist, as_of=NOW) is None
    result = maybe_log_entry_shadow(
        shortlist,
        as_of=NOW,
        decision_log=None,
        enabled=True,
        run_id="RUN-1",
    )
    assert result.status == "SKIPPED_SHORTLIST"
    assert result.advice is None
    assert result.decision is None


def test_disabled_leaves_live_path_unchanged(tmp_path: Path) -> None:
    store = TradingStore.open(tmp_path / "e.sqlite", clock=FrozenClock(NOW))
    log = DecisionLog(store)
    shortlist = _shortlist()
    result = maybe_log_entry_shadow(
        shortlist,
        as_of=NOW,
        decision_log=log,
        enabled=False,
        run_id="RUN-OFF",
    )
    assert result.status == "SKIPPED_DISABLED"
    assert result.advice is None
    assert log.list(role=DeskRole.ENTRY) == ()
    store.close()


def test_shadow_logged_when_enabled(tmp_path: Path) -> None:
    store = TradingStore.open(tmp_path / "e2.sqlite", clock=FrozenClock(NOW))
    log = DecisionLog(store)
    shortlist = _shortlist(("A", "0.6"), ("B", "0.9"))
    result = maybe_log_entry_shadow(
        shortlist,
        as_of=NOW,
        decision_log=log,
        enabled=True,
        run_id="RUN-ON",
        trade_id="TRD-9",
    )
    assert result.status == "LOGGED"
    assert result.advice is not None
    assert result.advice.action is AgentAction.SELECT_STRIKE_CANDIDATE
    assert result.advice.candidate_id == "B"
    assert result.decision is not None
    assert result.decision.gate_outcome is GateOutcome.SHADOW_ONLY
    assert result.decision.mode is AuthorityMode.SHADOW
    assert result.decision.role is DeskRole.ENTRY
    loaded = log.list(role=DeskRole.ENTRY)
    assert len(loaded) == 1
    assert loaded[0].decision_id == result.decision.decision_id
    assert loaded[0].action is AgentAction.SELECT_STRIKE_CANDIDATE
    store.close()


def test_size_multiplier_cannot_exceed_one() -> None:
    with pytest.raises(ValidationError):
        EntryAdvice(
            as_of=NOW,
            snapshot_id="SNAP-1",
            action=AgentAction.ABSTAIN,
            size_multiplier=Decimal("1.01"),
            narrative="too large",
        )
