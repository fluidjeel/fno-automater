"""Instrument and history backfill: chunking, fail-closed windows, persistence."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from tests.test_data_pipeline import FakeFeed, _history_capture, _underlying
from trading.data.backfill import (
    backfill_history,
    backfill_instruments,
    history_windows,
)
from trading.data.config import load_data_pipeline_config
from trading.data.events import RawMarketCapture
from trading.data.fyers.symbol_master import FyersSymbolMaster
from trading.data.storage.instrument_store import InstrumentSpecStore
from trading.data.storage.parquet_store import JsonlEventStore
from trading.domain.clock import FrozenClock
from trading.domain.enums import Exchange, InstrumentKind

REPO = Path(__file__).resolve().parent.parent
PIPELINE_CONFIG = REPO / "config" / "data_pipeline.yaml"
MASTER_FIXTURE = Path(__file__).parent / "fixtures" / "fyers_sym_master.json"


class _FixtureMaster:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def url_for(self, segment: str) -> str:
        return f"https://public.fyers.in/sym_details/{segment}_sym_master.json"

    def fetch(self, segment: str) -> RawMarketCapture:
        return RawMarketCapture(
            capture_id=f"master-{segment}",
            provider="fyers",
            endpoint=f"sym_details/{segment}",
            received_at=datetime(2026, 9, 13, 6, 0, tzinfo=UTC),
            payload=self._payload,
            http_status=200,
        )


class TestHistoryWindows:
    def test_windows_are_utc_aligned_and_idempotent(self) -> None:
        first = history_windows(end=date(2026, 9, 13), days=250, max_days=100)
        second = history_windows(end=date(2026, 9, 13), days=250, max_days=100)
        assert first == second
        assert first == (
            (date(2026, 1, 7), date(2026, 4, 16)),
            (date(2026, 4, 17), date(2026, 7, 25)),
            (date(2026, 7, 26), date(2026, 9, 13)),
        )

    def test_short_lookback_is_a_single_window(self) -> None:
        windows = history_windows(end=date(2026, 9, 13), days=5, max_days=100)
        assert windows == ((date(2026, 9, 9), date(2026, 9, 13)),)


class TestBackfillHistory:
    def test_unverified_resolution_fails_closed(self, tmp_path: Path) -> None:
        """An unpublished per-request window is not guessed."""
        now = datetime(2026, 9, 13, 6, 0, tzinfo=UTC)
        config = load_data_pipeline_config(PIPELINE_CONFIG)
        with pytest.raises(ValueError, match="history_max_days_by_resolution"):
            backfill_history(
                pipeline_config=config,
                repo_root=tmp_path,
                feed=FakeFeed(_history_capture(now)),
                store=JsonlEventStore(tmp_path / "data"),
                clock=FrozenClock(now),
                underlying=_underlying(),
                resolutions=("1S",),
                days=5,
                utc_today=date(2026, 9, 13),
            )

    def test_persists_canonical_bar_events(self, tmp_path: Path) -> None:
        now = datetime(2026, 9, 13, 6, 0, tzinfo=UTC)
        store = JsonlEventStore(tmp_path / "data")
        config = load_data_pipeline_config(PIPELINE_CONFIG)
        results = backfill_history(
            pipeline_config=config,
            repo_root=tmp_path,
            feed=FakeFeed(_history_capture(now)),
            store=store,
            clock=FrozenClock(now),
            underlying=_underlying(),
            resolutions=("5",),
            days=5,
            utc_today=date(2026, 9, 13),
        )
        assert len(results) == 1
        assert results[0].windows == ((date(2026, 9, 9), date(2026, 9, 13)),)
        events = store.read_canonical(
            symbol="NSE:NIFTY50-INDEX",
            start=datetime(2026, 9, 1, tzinfo=UTC),
            end=datetime(2026, 9, 14, tzinfo=UTC),
        )
        assert len(events) == 1
        assert events[0].event_type == "BAR_SNAPSHOT"
        assert events[0].event_id == results[0].event_ids[0]


class TestBackfillInstruments:
    def test_writes_catalog_from_a_fixture_master(self, tmp_path: Path) -> None:
        now = datetime(2026, 9, 13, 6, 0, tzinfo=UTC)
        payload = json.loads(MASTER_FIXTURE.read_text(encoding="utf-8"))
        config = load_data_pipeline_config(PIPELINE_CONFIG)
        store = JsonlEventStore(tmp_path / "data")
        results = backfill_instruments(
            pipeline_config=config.model_copy(
                update={
                    "reference": config.reference.model_copy(
                        update={"segments": ("NSE_FO",)}
                    )
                }
            ),
            repo_root=tmp_path,
            store=store,
            clock=FrozenClock(now),
            fetcher=_FixtureMaster(payload),  # type: ignore[arg-type]
        )
        assert results[0].segment == "NSE_FO"
        assert results[0].spec_count >= 3
        catalog = InstrumentSpecStore(
            tmp_path / config.storage.root / config.reference.instrument_subdir
        )
        index = catalog.find("NSE:NIFTY50-INDEX")
        assert index is not None
        assert index.instrument_kind is InstrumentKind.INDEX
        assert index.exchange is Exchange.NSE
        option = catalog.find("NSE:NIFTY2691519050CE")
        assert option is not None
        assert option.lot_size == 65


def test_symbol_master_url_is_templated() -> None:
    master = FyersSymbolMaster(
        FrozenClock(datetime(2026, 9, 13, 6, 0, tzinfo=UTC)),
        url_template="https://public.fyers.in/sym_details/{segment}_sym_master.json",
        timeout_seconds=1.0,
    )
    assert master.url_for("NSE_CM").endswith("NSE_CM_sym_master.json")
