"""ADESK-B9: FRAGILITY advisory narration over StressReport."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from trading.ai.fragility import build_fragility_telegram_line, maybe_log_fragility
from trading.analytics.stress import PositionStressInput, build_stress_report
from trading.domain.primitives import Currency, Money

NOW = datetime(2026, 9, 20, 15, 0, tzinfo=UTC)
INR = Currency.INR


def test_telegram_line_names_contributors() -> None:
    report = build_stress_report(
        as_of=NOW,
        snapshot_id="stress-1",
        equity=Money.of("1000000", INR),
        positions=(
            PositionStressInput(
                trade_id="t1",
                defined_risk_max_loss=Money.of("25000", INR),
                delta_pnl_per_pct=Money.of("0", INR),
                vega_pnl_per_pct=Money.of("0", INR),
                is_defined_risk=True,
            ),
        ),
        tail_budget_fraction=Decimal("0.08"),
    )
    narration = build_fragility_telegram_line(report)
    assert "FRAGILITY stress-1" in narration.telegram_line
    assert "worst_case=" in narration.telegram_line
    assert narration.contributor_ids


def test_disabled_skips_log() -> None:
    report = build_stress_report(
        as_of=NOW,
        snapshot_id="s2",
        equity=Money.of("1000000", INR),
        positions=(
            PositionStressInput(
                trade_id="t0",
                defined_risk_max_loss=Money.of("1000", INR),
                delta_pnl_per_pct=Money.of("0", INR),
                vega_pnl_per_pct=Money.of("0", INR),
                is_defined_risk=True,
            ),
        ),
        tail_budget_fraction=Decimal("0.08"),
    )
    result = maybe_log_fragility(report, decision_log=None, enabled=False, run_id="r")
    assert result.status == "SKIPPED_DISABLED"
