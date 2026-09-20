"""ADESK-B8: TradeAttribution dual-entry journal + four-cell report."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from trading.ai.posttrade import build_trade_attribution
from trading.domain.contracts.posttrade import (
    entry_journal_hash,
    four_cell_verdict_counts,
)
from trading.domain.enums import ThesisVerdict

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


def test_entry_journal_hash_verified_at_exit() -> None:
    h = entry_journal_hash(thesis_hash="abc", narrative="entry note")
    attr = build_trade_attribution(
        trade_id="t1",
        thesis_id="th1",
        thesis_hash="abc",
        entry_narrative="entry note",
        stored_entry_hash=h,
        outcome_r=Decimal("1.2"),
        mae_r=Decimal("0.3"),
        mfe_r=Decimal("1.5"),
        thesis_correct=True,
    )
    assert attr.thesis_hash_verified is True
    assert attr.thesis_verdict is ThesisVerdict.CORRECT_AND_PAID


def test_hash_mismatch_is_hard_error() -> None:
    with pytest.raises(ValueError, match="mismatch"):
        build_trade_attribution(
            trade_id="t1",
            thesis_id="th1",
            thesis_hash="abc",
            entry_narrative="entry note",
            stored_entry_hash="deadbeef",
            outcome_r=Decimal("1"),
            mae_r=Decimal("0.1"),
            mfe_r=Decimal("1"),
            thesis_correct=True,
        )


def test_four_cell_verdict_report() -> None:
    h = entry_journal_hash(thesis_hash="h", narrative="n")
    rows = (
        build_trade_attribution(
            trade_id="a",
            thesis_id="1",
            thesis_hash="h",
            entry_narrative="n",
            stored_entry_hash=h,
            outcome_r=Decimal("1"),
            mae_r=Decimal("0"),
            mfe_r=Decimal("1"),
            thesis_correct=True,
        ),
        build_trade_attribution(
            trade_id="b",
            thesis_id="2",
            thesis_hash="h",
            entry_narrative="n",
            stored_entry_hash=h,
            outcome_r=Decimal("-1"),
            mae_r=Decimal("1"),
            mfe_r=Decimal("0.2"),
            thesis_correct=False,
        ),
    )
    counts = four_cell_verdict_counts(rows)
    assert counts[ThesisVerdict.CORRECT_AND_PAID.value] == 1
    assert counts[ThesisVerdict.WRONG_AND_LOST.value] == 1
