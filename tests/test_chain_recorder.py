"""Tests for self-hosted option chain recorder."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trading.data.events import RawMarketCapture
from trading.data.recorder import OptionChainRecorder
from trading.domain.clock import FrozenClock

NOW = datetime(2026, 9, 20, 10, 0, 0, tzinfo=UTC)


class DummyFeed:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = (
            rows
            if rows is not None
            else [
                {
                    "symbol": "NSE:NIFTY2692424000CE",
                    "strike_price": 24000.0,
                    "option_type": "CE",
                    "ltp": 150.5,
                    "bid": 150.0,
                    "ask": 151.0,
                    "volume": 20000,
                    "oi": 50000,
                    "iv": 14.5,
                },
                {
                    "symbol": "NSE:NIFTY2692424000PE",
                    "strike_price": 24000.0,
                    "option_type": "PE",
                    "ltp": 95.0,
                    "bid": 94.5,
                    "ask": 95.5,
                    "volume": 18000,
                    "oi": 42000,
                    "iv": 15.2,
                },
            ]
        )

    def fetch_option_chain(self, symbol: str) -> RawMarketCapture:
        return RawMarketCapture(
            capture_id="cap-1",
            provider="fyers",
            endpoint="options-chain-v3",
            received_at=NOW,
            payload={"s": "ok", "data": {"optionsChain": self.rows}},
            http_status=200,
        )


def test_option_chain_recorder_writes_and_reads_parquet(tmp_path: Path) -> None:
    feed = DummyFeed()
    clock = FrozenClock(NOW)
    recorder = OptionChainRecorder(feed=feed, output_dir=tmp_path, clock=clock)

    path = recorder.record_chain("NSE:NIFTY50-INDEX")

    assert path.exists()
    assert "underlying=NSE_NIFTY50-INDEX" in str(path)
    assert "date=2026-09-20" in str(path)
    assert path.name == "chain_100000.parquet"

    df = recorder.load_chain(path)
    assert df.height == 2
    assert "recorded_at" in df.columns
    assert "underlying_symbol" in df.columns
    assert df["underlying_symbol"][0] == "NSE:NIFTY50-INDEX"
    assert df["strike_price"].to_list() == [24000.0, 24000.0]


def test_option_chain_recorder_empty_payload(tmp_path: Path) -> None:
    feed = DummyFeed(rows=[])
    clock = FrozenClock(NOW)
    recorder = OptionChainRecorder(feed=feed, output_dir=tmp_path, clock=clock)

    path = recorder.record_chain("NSE:NIFTYBANK-INDEX")
    assert path.exists()
    df = recorder.load_chain(path)
    assert df.height == 1
    assert "empty" in df.columns
    assert df["empty"][0] is True


def test_poll_and_record_iterations(tmp_path: Path) -> None:
    feed = DummyFeed()
    clock = FrozenClock(NOW)
    recorder = OptionChainRecorder(feed=feed, output_dir=tmp_path, clock=clock)

    recorded: list[Path] = []
    paths = recorder.poll_and_record(
        ["NSE:NIFTY50-INDEX", "NSE:NIFTYBANK-INDEX"],
        interval_seconds=0,
        max_iterations=1,
        on_record=recorded.append,
    )

    assert len(paths) == 2
    assert len(recorded) == 2
    assert all(p.exists() for p in paths)
