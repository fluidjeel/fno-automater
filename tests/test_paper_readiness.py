"""Tests for paper readiness evaluation engine (PAPER-005)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from trading.analytics.paper_readiness import (
    evaluate_paper_readiness,
    format_paper_readiness,
)

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


def test_paper_readiness_insufficient_sample() -> None:
    report = evaluate_paper_readiness(
        total_decisions=50,
        charges_verified=True,
        live_config_verified=True,
        as_of=NOW,
    )
    assert report.is_promotion_eligible is False
    assert report.sample_sufficient is False
    assert any("INSUFFICIENT_SAMPLE" in b for b in report.blockers)


def test_paper_readiness_multiple_blockers() -> None:
    report = evaluate_paper_readiness(
        total_decisions=160,
        charges_verified=False,
        live_config_verified=False,
        safety_violations=2,
        brier_score=Decimal("0.35"),
        as_of=NOW,
    )
    assert report.is_promotion_eligible is False
    assert report.sample_sufficient is True
    assert len(report.blockers) == 4
    assert any("CHARGES_UNVERIFIED" in b for b in report.blockers)
    assert any("LIVE_CONFIG_UNVERIFIED" in b for b in report.blockers)
    assert any("SAFETY_VIOLATIONS" in b for b in report.blockers)
    assert any("BRIER_POOR" in b for b in report.blockers)


def test_paper_readiness_eligible() -> None:
    report = evaluate_paper_readiness(
        total_decisions=155,
        charges_verified=True,
        live_config_verified=True,
        safety_violations=0,
        brier_score=Decimal("0.18"),
        as_of=NOW,
    )
    assert report.is_promotion_eligible is True
    assert len(report.blockers) == 0

    text = format_paper_readiness(report)
    assert "ELIGIBLE FOR CANARY" in text
    assert "155 / 150" in text
