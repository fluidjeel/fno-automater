"""Tests for M2 agentic design fixes (storage, readiness, bar stats, JSON)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from trading.data.features.bar_stats_cache import apply_bars, load_bar_stats, vwap_from_stats
from trading.data.features.compute import compute_feature_row
from trading.data.m2_agentic_pipeline.io import atomic_json_write, atomic_jsonl_append
from trading.data.m2_agentic_pipeline.readiness import wait_for_minute_pipeline
from trading.data.minute_backbone.models import MinuteBundle, MinuteStatus
from trading.data.tick_consumer.models import Bar1m
from trading.data.tick_consumer.store import TickStore
from trading.domain.clock import FrozenClock

_IST = ZoneInfo("Asia/Kolkata")


def test_jsonl_append_is_append_only(tmp_path: Path) -> None:
    path = tmp_path / "ticks.jsonl"
    atomic_jsonl_append(path, [{"n": 1}])
    first_size = path.stat().st_size
    with patch("polars.read_parquet") as read_parquet:
        atomic_jsonl_append(path, [{"n": 2}])
        read_parquet.assert_not_called()
    assert path.stat().st_size > first_size
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2


def test_compact_json_write(tmp_path: Path) -> None:
    target = tmp_path / "row.json"
    atomic_json_write(target, {"a": 1, "b": [2, 3]})
    raw = target.read_text(encoding="utf-8")
    assert "\n  " not in raw
    assert json.loads(raw) == {"a": 1, "b": [2, 3]}


def test_bar_stats_cache_avoids_parquet_scan(tmp_path: Path) -> None:
    bar = Bar1m(
        symbol="NSE:NIFTY50-INDEX",
        bar_start_ist=datetime(2026, 10, 31, 9, 20, tzinfo=_IST),
        open="100",
        high="105",
        low="95",
        close="102",
        volume=1000,
        buy_volume=600,
        sell_volume=400,
        unknown_volume=0,
        tick_count=50,
    )
    apply_bars(tmp_path, [bar], session_date="2026-10-31")
    stats = load_bar_stats(tmp_path, "2026-10-31")
    assert stats is not None
    assert vwap_from_stats(stats) != "0"
    bundle = MinuteBundle.model_validate(
        {
            "schema_version": "1",
            "minute_start_ist": datetime(2026, 10, 31, 9, 20, tzinfo=_IST).isoformat(),
            "status": MinuteStatus.OK.value,
            "nifty_quote": {"ltp": "102", "bid": "101", "ask": "103", "as_of_utc": "2026-10-31T03:50:00+00:00"},
            "vix_quote": {"ltp": "12", "bid": "12", "ask": "12", "as_of_utc": "2026-10-31T03:50:00+00:00"},
            "chain_snapshot_id": "c1",
            "depth_snapshot_id": "d1",
            "chain": {"strikes": []},
            "depth": {"spread_bps": "1"},
        }
    )
    with patch("trading.data.features.compute._bars_frame") as read_bars:
        row = compute_feature_row(tmp_path, bundle, config=_minimal_config(tmp_path))
        read_bars.assert_not_called()
    assert row["vwap"]["session"] != "0"


def test_wait_for_minute_pipeline_times_out(tmp_path: Path) -> None:
    mark = datetime(2026, 10, 31, 9, 20, tzinfo=_IST)
    result = wait_for_minute_pipeline(tmp_path, mark, max_lag_seconds=0, poll_interval_seconds=0.01)
    assert result is None


def test_tick_store_writes_jsonl_shards(tmp_path: Path) -> None:
    session = datetime(2026, 10, 31, 9, 20, tzinfo=_IST).date()
    store = TickStore(tmp_path)
    ts = datetime(2026, 10, 31, 9, 20, tzinfo=_IST).astimezone(UTC)
    from trading.data.tick_consumer.models import SignedTick, TickSign

    store.append_tick(
        SignedTick(
            symbol="NSE:NIFTY50-INDEX",
            ltp="24500",
            volume=10,
            trade_size=5,
            exchange_ts=ts,
            received_at=ts,
            sign=TickSign.BUY,
        ),
        session_date=session,
    )
    store.flush_ticks(session_date=session)
    shard = tmp_path / "data/m2_agentic/ticks/2026-10-31/h09/ticks.jsonl"
    assert shard.is_file()


def _minimal_config(tmp_path: Path):  # noqa: ANN202
    from trading.data.m2_agentic_pipeline.config import load_m2_agentic_pipeline_config
    import shutil

    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    repo = Path(__file__).resolve().parents[1]
    shutil.copy(repo / "config/m2_agentic_pipeline.yaml", tmp_path / "config/m2_agentic_pipeline.yaml")
    return load_m2_agentic_pipeline_config(tmp_path / "config/m2_agentic_pipeline.yaml")
