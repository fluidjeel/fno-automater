"""Breakout simulation: 30 judgment-aware cells over real binders and router."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from trading.analytics.judgment import label_excursion
from trading.config.evaluation import load_evaluation_config
from trading.identification.breakout_scenarios import (
    BreakoutBucket,
    BreakoutRow,
    ChainQuality,
    JudgmentVerdict,
    format_breakout_report,
    persist_breakout_report,
    run_breakout_scenarios,
    scoreboard,
    verdict_for,
)
from trading.identification.config import load_identification_policy

ROOT = Path(__file__).parents[1]
POLICY = load_identification_policy(ROOT / "config" / "identification.yaml")
EVALUATION = load_evaluation_config(ROOT / "config" / "evaluation.yaml").config


@pytest.fixture(scope="module")
def rows() -> tuple[BreakoutRow, ...]:
    return run_breakout_scenarios(POLICY, EVALUATION)


def test_breakout_matrix_has_ten_cells_per_bucket(
    rows: tuple[BreakoutRow, ...],
) -> None:
    """Exactly 10 CAS + 10 positional + 10 direction with a judgment label."""
    assert len(rows) == 30
    counts = {bucket.value: 0 for bucket in BreakoutBucket}
    for row in rows:
        counts[row.bucket.value] += 1
        assert row.judgment_label in {"should_enter", "should_pass"}
        assert row.verdict is not JudgmentVerdict.NO_LABEL
    assert counts == {"CAS": 10, "positional": 10, "direction": 10}


def test_breakout_verdicts_cover_enter_loss_pass_and_miss(
    rows: tuple[BreakoutRow, ...],
) -> None:
    """Decision quality is MAE/MFE minus charges, not merely whether a family routed."""
    verdicts = {row.verdict for row in rows}
    assert JudgmentVerdict.CORRECT_ENTER in verdicts
    assert JudgmentVerdict.LOSS_SHOULD_HAVE_PASSED in verdicts
    assert JudgmentVerdict.CORRECT_PASS in verdicts
    assert JudgmentVerdict.MISSED in verdicts
    by_name = {row.name: row for row in rows}
    assert by_name["cas_open_continuation_up"].verdict is JudgmentVerdict.CORRECT_ENTER
    assert by_name["cas_open_false_break_fade"].verdict is (
        JudgmentVerdict.LOSS_SHOULD_HAVE_PASSED
    )
    assert by_name["cas_wide_auction_cross"].verdict is JudgmentVerdict.CORRECT_PASS
    assert by_name["pos_thin_mfe_after_charges"].verdict is (
        JudgmentVerdict.LOSS_SHOULD_HAVE_PASSED
    )
    assert by_name["dir_missed_delta_outside_band"].verdict is JudgmentVerdict.MISSED
    assert by_name["dir_up_false_break"].verdict is (
        JudgmentVerdict.LOSS_SHOULD_HAVE_PASSED
    )
    assert by_name["dir_down_false_break"].verdict is (
        JudgmentVerdict.LOSS_SHOULD_HAVE_PASSED
    )


def test_paper_fail_fast_resolves_direction_score_tie(
    rows: tuple[BreakoutRow, ...],
) -> None:
    """Equal binder scores still name the preferred family and log min_score_gap."""
    row = {item.name: item for item in rows}["dir_score_tie_fail_fast"]
    assert row.paper_winner == "positional_long_option"
    assert row.forced_choice is True
    assert "min_score_gap" in row.failed_gates
    assert row.verdict is JudgmentVerdict.CORRECT_ENTER


def test_cas_auction_and_positional_tenor_and_event_cells(
    rows: tuple[BreakoutRow, ...],
) -> None:
    """CAS stays in auction windows; positional is monthly; blackout cells are rare."""
    by_name = {row.name: row for row in rows}
    assert by_name["cas_open_continuation_up"].regime["session"] == "AUCTION"
    assert by_name["cas_open_continuation_up"].paper_winner == "cas_microstructure"
    assert by_name["cas_macro_conflict_chop"].paper_winner == "cas_microstructure"
    assert by_name["cas_macro_conflict_chop"].binder_eligible is True
    assert by_name["pos_compression_break_call"].regime["dte"] == "28"
    assert by_name["pos_compression_break_call"].paper_tenor == "POSITIONAL"
    assert by_name["pos_high_iv_debit_call"].paper_winner == "debit_spread"
    blackout = [
        row.name
        for row in rows
        if row.regime["event_state"] == "BLOCK_NEW"
        or row.regime["macro_status"] == "CONFLICT"
    ]
    assert blackout == ["cas_macro_conflict_chop", "pos_block_new_event_chop"]


def test_binders_reject_wide_spread_and_thin_oi(
    rows: tuple[BreakoutRow, ...],
) -> None:
    """Thin book / wide auction cross is a binder pass, not a routed win."""
    by_name = {row.name: row for row in rows}
    assert by_name["cas_wide_auction_cross"].entered is False
    assert by_name["cas_thin_oi_reject"].entered is False
    assert by_name["dir_thin_liquidity_weekly"].entered is False
    assert by_name["pos_wide_spread_monthly"].entered is False
    assert (
        by_name["cas_wide_auction_cross"].regime["quality"] == ChainQuality.WIDE.value
    )


def test_verdict_helper_and_label_excursion_match_evaluation_yaml() -> None:
    """Net MFE after ₹100/lot charges is the enter/pass boundary."""
    charges = EVALUATION.fill_model.charges_per_lot.require("charges")
    assert charges == Decimal("100")
    assert (
        label_excursion(
            mae=Decimal("40"),
            mfe=Decimal("80"),
            lots=1,
            charges_per_lot=charges,
            thresholds=EVALUATION.judgment,
        )
        is False
    )
    assert (
        label_excursion(
            mae=Decimal("900"),
            mfe=Decimal("5200"),
            lots=1,
            charges_per_lot=charges,
            thresholds=EVALUATION.judgment,
        )
        is True
    )
    assert verdict_for(entered=True, should_enter=True) is JudgmentVerdict.CORRECT_ENTER
    assert (
        verdict_for(entered=True, should_enter=False)
        is JudgmentVerdict.LOSS_SHOULD_HAVE_PASSED
    )
    assert (
        verdict_for(entered=False, should_enter=False) is JudgmentVerdict.CORRECT_PASS
    )
    assert verdict_for(entered=False, should_enter=True) is JudgmentVerdict.MISSED
    assert verdict_for(entered=False, should_enter=None) is JudgmentVerdict.NO_LABEL


def test_scoreboard_and_persist_json(
    rows: tuple[BreakoutRow, ...], tmp_path: Path
) -> None:
    """Scoreboard precision is wins/(wins+losses); JSON round-trips the table."""
    board = scoreboard(rows)
    assert board["overall"]["n"] == 30
    wins = board["overall"]["wins"]
    losses = board["overall"]["losses"]
    assert wins + losses > 0
    expected = (Decimal(wins) / Decimal(wins + losses)).quantize(Decimal("0.0001"))
    assert board["overall"]["precision"] == str(expected)
    path = tmp_path / "breakout_scenarios.json"
    persist_breakout_report(
        rows, path, generated_at=datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    )
    text = path.read_text(encoding="utf-8")
    assert '"schema": "breakout-simulation-v1"' in text
    assert "cas_open_continuation_up" in text
    report = format_breakout_report(rows)
    assert "=== CAS (10) ===" in report
    assert "=== scoreboard ===" in report
