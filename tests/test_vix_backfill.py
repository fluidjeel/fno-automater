from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from trading.data.config import load_data_pipeline_config
from trading.data.events import RawMarketCapture
from trading.data.storage.snapshot_store import SnapshotStore
from trading.data.vix_backfill import backfill_vix_snapshots, vix_underlying
from trading.domain.clock import FrozenClock
from trading.runtime.paper_session import _vix_history

PIPELINE_CONFIG = Path("config/data_pipeline.yaml")
BASE_CONFIG = Path("config/paper.yaml")


class _DailyVixFeed:
    def fetch_option_chain(self, symbol: str) -> RawMarketCapture:
        raise NotImplementedError(symbol)

    def fetch_quotes(self, symbols: tuple[str, ...]) -> RawMarketCapture:
        raise NotImplementedError(symbols)

    def fetch_depth(self, symbol: str) -> RawMarketCapture:
        raise NotImplementedError(symbol)

    def fetch_market_status(self) -> RawMarketCapture:
        raise NotImplementedError

    def fetch_expiry_dates(self, symbol: str) -> RawMarketCapture:
        raise NotImplementedError(symbol)

    def fetch_history(
        self,
        symbol: str,
        *,
        resolution: str,
        range_from: str,
        range_to: str,
    ) -> RawMarketCapture:
        assert resolution == "D"
        base = datetime(2026, 9, 22, tzinfo=UTC) - timedelta(days=30)
        candles = [
            [
                int((base + timedelta(days=offset)).timestamp()),
                "12",
                "13",
                "11",
                str(11 + offset),
                0,
            ]
            for offset in range(25)
        ]
        return RawMarketCapture(
            capture_id="vix-daily",
            provider="fyers",
            endpoint="history",
            received_at=datetime(2026, 9, 22, 6, 0, tzinfo=UTC),
            payload={"candles": candles},
            http_status=200,
        )


def test_backfill_vix_snapshots_populates_warmup_history(tmp_path: Path) -> None:
    pipeline_config = load_data_pipeline_config(PIPELINE_CONFIG)
    underlying = vix_underlying(pipeline_config)
    snapshot_root = tmp_path / "data" / "snapshots"
    now = datetime(2026, 9, 22, 6, 0, tzinfo=UTC)
    result = backfill_vix_snapshots(
        pipeline_config=pipeline_config,
        repo_root=tmp_path,
        feed=_DailyVixFeed(),
        clock=FrozenClock(now),
        app_config_path=BASE_CONFIG,
        days=45,
    )
    assert result.written >= 20
    assert result.trading_days >= 20
    history = _vix_history(
        SnapshotStore(snapshot_root),
        symbol=underlying.symbol,
        now=now,
    )
    assert len(history) >= 20
