"""Macro news JSONL loading, scoring and CLI helpers (offline)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from trading.data.macro_news import (
    MacroNewsItem,
    format_macro_summary,
    load_macro_news_jsonl,
    score_macro_news,
)

NOW = datetime(2024, 9, 13, 6, 0, tzinfo=UTC)


def _item(
    event_id: str,
    *,
    scope: str = "NIFTY",
    sentiment: int = 1,
) -> MacroNewsItem:
    return MacroNewsItem(
        event_id=event_id,
        source="verified-source",
        scope=scope,
        published_at=NOW,
        received_at=NOW,
        sentiment=sentiment,  # type: ignore[arg-type]
        impact=Decimal("0.8"),
        confidence=Decimal("0.9"),
        evidence_ref=f"data/raw/news/{event_id}.json",
    )


class TestLoadMacroNewsJsonl:
    def test_skips_bad_lines_and_keeps_valid_records(self, tmp_path: Path) -> None:
        path = tmp_path / "macro_news.jsonl"
        good = _item("NEWS-OK").model_dump_json()
        path.write_text(f"{good}\nnot valid json\n", encoding="utf-8")
        loaded = load_macro_news_jsonl(path)
        assert len(loaded.items) == 1
        assert loaded.items[0].event_id == "NEWS-OK"
        assert len(loaded.errors) == 1
        assert "line 2" in loaded.errors[0]

    def test_skips_duplicate_event_ids(self, tmp_path: Path) -> None:
        path = tmp_path / "macro_news.jsonl"
        line = _item("NEWS-DUP").model_dump_json()
        path.write_text(f"{line}\n{line}\n", encoding="utf-8")
        loaded = load_macro_news_jsonl(path)
        assert len(loaded.items) == 1
        assert loaded.skipped_duplicates == ("NEWS-DUP",)

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        loaded = load_macro_news_jsonl(tmp_path / "missing.jsonl")
        assert loaded.items == ()
        assert loaded.errors == ()


class TestScoreMacroNews:
    def test_global_scope_contributes_to_underlying_score(self) -> None:
        items = (_item("G1", scope="GLOBAL", sentiment=-1),)
        factor = score_macro_news(
            items,
            scope="NIFTY",
            as_of=NOW,
            max_age_seconds=86_400,
            half_life_seconds=86_400,
            calculation_version="test-v1",
        )
        assert factor is not None
        assert factor.sentiment == -1
        assert factor.event_count == 1


class TestFormatMacroSummary:
    def test_none_factor(self) -> None:
        assert format_macro_summary(None) == "macro=none"

    def test_factor_with_events(self) -> None:
        factor = score_macro_news(
            (_item("NEWS-1"),),
            scope="NIFTY",
            as_of=NOW,
            max_age_seconds=86_400,
            half_life_seconds=86_400,
            calculation_version="test-v1",
        )
        assert factor is not None
        summary = format_macro_summary(factor)
        assert summary.startswith("macro=1events sentiment=")
