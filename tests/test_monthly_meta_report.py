"""ADESK-E3: monthly meta-report — scorecards, demotions, det-vs-desk."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from trading.analytics.agent_scorecard import build_agent_scorecard
from trading.analytics.monthly_meta_report import (
    DemotionEvent,
    DetVsDeskRow,
    build_monthly_meta_report,
    month_id_for,
    persist_monthly_meta_report,
    render_monthly_meta_markdown,
)
from trading.domain.enums import AuthorityMode, DemotionReason, DeskRole

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


def _card(role: DeskRole):
    return build_agent_scorecard((), role=role, as_of=NOW)


def test_month_id() -> None:
    assert month_id_for(NOW) == "2026-09"


def test_build_aggregates_scorecards_demotions_and_comparison() -> None:
    demotions = (
        DemotionEvent(
            event_id="d1",
            role=DeskRole.ENTRY,
            at=NOW - timedelta(days=3),
            from_mode=AuthorityMode.ADVISORY,
            to_mode=AuthorityMode.SHADOW,
            reason=DemotionReason.HALLUCINATION_RATE,
            narrative="rolling hallucination breached band",
        ),
    )
    rows = (
        DetVsDeskRow(
            role=DeskRole.ENTRY,
            sample_size=40,
            det_only_mean_r=Decimal("0.10"),
            desk_mean_r=Decimal("0.05"),
            delta_r=Decimal("-0.05"),
            narrative="desk underperformed det-only",
        ),
    )
    report = build_monthly_meta_report(
        as_of=NOW,
        scorecards=(_card(DeskRole.ENTRY), _card(DeskRole.RESEARCH)),
        demotions=demotions,
        det_vs_desk=rows,
    )
    assert report.month_id == "2026-09"
    assert report.demotion_count == 1
    assert len(report.desk_scorecards) == 2
    assert len(report.det_vs_desk) == 1
    assert report.attention_required is True  # negative delta with N>=20


def test_demotion_rejects_authority_upgrade() -> None:
    with pytest.raises(ValidationError, match="demotion"):
        DemotionEvent(
            event_id="bad",
            role=DeskRole.POSITION,
            at=NOW,
            from_mode=AuthorityMode.OBSERVE,
            to_mode=AuthorityMode.BOUNDED,
            reason=DemotionReason.MANUAL,
        )


def test_persist_writes_json_and_markdown(tmp_path: Path) -> None:
    report = build_monthly_meta_report(
        as_of=NOW,
        scorecards=(_card(DeskRole.ENTRY),),
    )
    md = persist_monthly_meta_report(report, out_dir=tmp_path)
    assert md.exists()
    assert "monthly_meta_2026-09.md" in md.name
    text = md.read_text(encoding="utf-8")
    assert "Agent Desk monthly meta-report" in text
    assert "Desk scorecards" in text
    assert (tmp_path / "monthly_meta_2026-09.json").exists()
    assert (tmp_path / "monthly_meta_latest.json").exists()


def test_markdown_readable_sections() -> None:
    report = build_monthly_meta_report(
        as_of=NOW,
        scorecards=(_card(DeskRole.ENTRY),),
        demotions=(
            DemotionEvent(
                event_id="d2",
                role=DeskRole.ENTRY,
                at=NOW,
                from_mode=AuthorityMode.BOUNDED,
                to_mode=AuthorityMode.OBSERVE,
                reason=DemotionReason.SAFETY_INVARIANT,
            ),
        ),
        det_vs_desk=(
            DetVsDeskRow(
                role=DeskRole.ENTRY,
                sample_size=10,
                det_only_mean_r=Decimal("0.2"),
                desk_mean_r=Decimal("0.25"),
                delta_r=Decimal("0.05"),
            ),
        ),
    )
    md = render_monthly_meta_markdown(report)
    assert "## Desk scorecards" in md
    assert "## Demotions" in md
    assert "## Deterministic vs Desk" in md
    assert report.attention_required is True  # SAFETY_INVARIANT → OBSERVE
