"""ADESK-E1: RESEARCH weekly runner — clusters, bias, playbook proposals."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from trading.analytics.bias import TradeOutcomeInput
from trading.analytics.improvements import normalize_claim_key
from trading.analytics.research_weekly import (
    build_playbook_proposals,
    build_research_weekly,
    persist_research_weekly,
    week_id_for,
)
from trading.domain.contracts.improvement import ImprovementRecord
from trading.domain.contracts.research import PlaybookEditProposal
from trading.domain.enums import (
    DeskRole,
    ImprovementArea,
    PlaybookEditKind,
    PlaybookTriggerKind,
)
from trading.domain.enums import TestabilityKind as ClaimTestability

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


def _record(
    record_id: str,
    *,
    claim: str = "exits cut winners early",
    area: ImprovementArea = ImprovementArea.EXIT_RULE,
    trades: tuple[str, ...] = ("t1",),
    cost: str = "0.5",
    occurrences: int = 1,
) -> ImprovementRecord:
    return ImprovementRecord(
        record_id=record_id,
        opened_at=NOW - timedelta(days=7),
        author=DeskRole.POSTTRADE,
        area=area,
        claim=claim,
        supporting_trade_ids=trades,
        proposed_change="raise trail after +1R",
        testable_as=ClaimTestability.SHADOW_RULE,
        occurrences=occurrences,
        estimated_cost_r=Decimal(cost),
        claim_key=normalize_claim_key(area, claim),
    )


def _trade(i: int, *, winner: bool) -> TradeOutcomeInput:
    return TradeOutcomeInput(
        trade_id=f"t{i}",
        is_winner=winner,
        hold_minutes=Decimal("120" if winner else "40"),
        capture_ratio=Decimal("0.55" if winner else "0"),
        pnl_r=Decimal("1.0" if winner else "-0.8"),
        execution_cost_r=Decimal("0.10"),
        closed_at=NOW + timedelta(hours=i),
        entry_weekday=0,
        entry_hour=10,
        iv_bucket="MID",
        supporting_reason_count=2,
        contradicting_reason_count=1,
        entry_confidence=Decimal("0.70"),
        posttrade_thesis_verdict=Decimal("0.60"),
        size_multiplier=Decimal("1.0"),
    )


def test_week_id_iso() -> None:
    assert week_id_for(NOW) == "2026-W38"


def test_build_ranks_clusters_and_proposes_playbook() -> None:
    records = (
        _record("r1", claim="A", cost="1.0", occurrences=2),
        _record("r2", claim="A", cost="1.0", occurrences=1),
        _record(
            "r3",
            claim="risk limit tight",
            area=ImprovementArea.RISK_LIMIT,
            cost="2.0",
            occurrences=2,
        ),
    )
    report = build_research_weekly(
        records,
        as_of=NOW,
        cohort_id="fixture-e1",
        trades=(_trade(0, winner=True), _trade(1, winner=False)),
    )
    assert report.week_id == "2026-W38"
    assert report.cluster_count == 2
    assert (
        report.ranked_clusters[0].rank_score
        >= report.ranked_clusters[1].rank_score
    )
    assert report.bias_report is not None
    assert len(report.bias_report.metrics) == 11
    assert any(p.area is ImprovementArea.RISK_LIMIT for p in report.playbook_proposals)
    assert all(p.auto_implement is False for p in report.playbook_proposals)


def test_playbook_proposal_rejects_auto_implement() -> None:
    with pytest.raises(ValidationError, match="auto-implement"):
        PlaybookEditProposal(
            proposal_id="x",
            trigger=PlaybookTriggerKind.VIX_JUMP,
            edit_kind=PlaybookEditKind.ADD_RESPONSE,
            area=ImprovementArea.RISK_LIMIT,
            auto_implement=True,
        )


def test_persist_writes_json(tmp_path: Path) -> None:
    report = build_research_weekly(
        (_record("r1", occurrences=3),),
        as_of=NOW,
        cohort_id="persist-e1",
    )
    path = persist_research_weekly(report, out_dir=tmp_path)
    assert path.exists()
    assert "research_weekly_2026-W38" in path.name
    payload = path.read_text(encoding="utf-8")
    assert "ranked_clusters" in payload
    assert (tmp_path / "research_weekly_latest.json").exists()


def test_empty_records_still_emits_report() -> None:
    report = build_research_weekly((), as_of=NOW, cohort_id="empty")
    assert report.cluster_count == 0
    assert report.playbook_proposals == ()
    assert report.bias_report is None


def test_build_playbook_from_bias_breach() -> None:
    trades = (
        TradeOutcomeInput(
            trade_id="w1",
            is_winner=True,
            hold_minutes=Decimal("20"),
            capture_ratio=Decimal("0.3"),
            pnl_r=Decimal("1.0"),
            execution_cost_r=Decimal("0.1"),
            closed_at=NOW,
            entry_weekday=0,
            entry_hour=10,
            iv_bucket="MID",
            supporting_reason_count=2,
            contradicting_reason_count=1,
            entry_confidence=Decimal("0.7"),
            posttrade_thesis_verdict=Decimal("0.6"),
            size_multiplier=Decimal("1.0"),
        ),
        TradeOutcomeInput(
            trade_id="l1",
            is_winner=False,
            hold_minutes=Decimal("200"),
            capture_ratio=Decimal("0"),
            pnl_r=Decimal("-1.0"),
            execution_cost_r=Decimal("0.1"),
            closed_at=NOW,
            entry_weekday=0,
            entry_hour=10,
            iv_bucket="MID",
            supporting_reason_count=2,
            contradicting_reason_count=1,
            entry_confidence=Decimal("0.7"),
            posttrade_thesis_verdict=Decimal("0.4"),
            size_multiplier=Decimal("1.0"),
        ),
    )
    report = build_research_weekly(
        (),
        as_of=NOW,
        cohort_id="bias-breach",
        trades=trades,
    )
    assert report.bias_report is not None
    proposals = build_playbook_proposals((), report.bias_report)
    breached = [m for m in report.bias_report.metrics if m.band_breached]
    if breached:
        assert any(p.trigger is PlaybookTriggerKind.BIAS_BAND_BREACH for p in proposals)
