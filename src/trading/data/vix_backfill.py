"""Backfill India VIX daily closes into the snapshot store for warmup gates."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from trading.config import load_config
from trading.data.backfill import _max_days, history_windows
from trading.data.config import DataPipelineConfig, UnderlyingConfig
from trading.data.cycle import build_cycle_snapshot
from trading.data.events import CanonicalMarketEvent
from trading.data.ports import MarketFeedPort
from trading.data.snapshot_builder import MarketSnapshotBuilder
from trading.data.storage.snapshot_store import SnapshotRecord, SnapshotStore
from trading.domain.clock import Clock
from trading.domain.contracts import DataQualityReport
from trading.domain.enums import DataQuality

__all__ = ["VixBackfillResult", "backfill_vix_snapshots", "vix_underlying"]

_IST = ZoneInfo("Asia/Kolkata")
_VIX_SYMBOL = "NSE:INDIAVIX-INDEX"
_DAILY_RESOLUTION = "D"
_MIN_CANDLE_FIELDS = 5


@dataclass(frozen=True, slots=True)
class VixBackfillResult:
    """Outcome of one India VIX snapshot backfill."""

    symbol: str
    written: int
    skipped: int
    trading_days: int


def vix_underlying(config: DataPipelineConfig) -> UnderlyingConfig:
    """Resolve the configured India VIX underlying."""
    for underlying in config.underlyings:
        if underlying.symbol == _VIX_SYMBOL:
            return underlying
    raise ValueError(f"{_VIX_SYMBOL!r} is not configured in data_pipeline.yaml")


def backfill_vix_snapshots(
    *,
    pipeline_config: DataPipelineConfig,
    repo_root: Path,
    feed: MarketFeedPort,
    clock: Clock,
    app_config_path: Path,
    days: int = 45,
) -> VixBackfillResult:
    """Fetch daily VIX history and append one snapshot record per trading day."""
    if days < 1:
        raise ValueError("days must be at least 1")
    underlying = vix_underlying(pipeline_config)
    snapshot_root = (
        repo_root
        / pipeline_config.storage.root
        / pipeline_config.storage.snapshot_subdir
    )
    snapshots = SnapshotStore(snapshot_root)
    existing = _existing_vix_dates(snapshots, underlying.symbol)
    loaded = load_config(app_config_path)
    builder = MarketSnapshotBuilder(
        underlying,
        config_version=loaded.version,
        config_checksum=loaded.checksum,
        code_version="vix-backfill",
    )
    quality = DataQualityReport(
        state=DataQuality.VALID,
        age_ms=0,
        warmup_complete=True,
        source_status="vix_backfill",
        reason_codes=(),
    )
    end = clock.now_utc().astimezone(UTC).date()
    max_days = _max_days(pipeline_config.reference, _DAILY_RESOLUTION)
    windows = history_windows(end=end, days=days, max_days=max_days)
    written = 0
    skipped = 0
    for start, stop in windows:
        capture = feed.fetch_history(
            underlying.symbol,
            resolution=_DAILY_RESOLUTION,
            range_from=start.isoformat(),
            range_to=stop.isoformat(),
        )
        candles = capture.payload.get("candles", [])
        if not isinstance(candles, list):
            continue
        for row in candles:
            if not isinstance(row, list) or len(row) < _MIN_CANDLE_FIELDS:
                continue
            try:
                stamp = datetime.fromtimestamp(int(row[0]), tz=UTC)
                close = Decimal(str(row[4]))
            except (TypeError, ValueError, ArithmeticError, OSError):
                continue
            if close <= 0:
                continue
            session_day = stamp.astimezone(_IST).date()
            if session_day in existing:
                skipped += 1
                continue
            as_of = datetime.combine(
                session_day,
                time(15, 30),
                tzinfo=_IST,
            ).astimezone(UTC)
            events = _synthetic_vix_events(
                underlying.symbol,
                close=close,
                bar_timestamp=stamp,
                as_of=as_of,
                normalization_version=pipeline_config.normalization_version,
            )
            snapshot = build_cycle_snapshot(
                events,
                quality=quality,
                as_of=as_of,
                builder=builder,
                index_only=True,
            )
            if snapshot is None:
                continue
            record = SnapshotRecord.for_cycle(
                symbol=underlying.symbol,
                as_of=as_of,
                quality=quality,
                event_ids=tuple(event.event_id for event in events),
                snapshot=snapshot,
            )
            snapshots.append(record)
            existing.add(session_day)
            written += 1
    return VixBackfillResult(
        symbol=underlying.symbol,
        written=written,
        skipped=skipped,
        trading_days=len(existing),
    )


def _existing_vix_dates(store: SnapshotStore, symbol: str) -> set[date]:
    dates: set[date] = set()
    for record in store.read(symbol=symbol):
        if record.snapshot is None:
            continue
        level = record.snapshot.features.get("india_vix")
        if level is None or level <= 0:
            last = record.snapshot.market.last
            close = record.snapshot.market.close
            price = (
                last.value
                if last is not None
                else (None if close is None else close.value)
            )
            if price is None or price <= 0:
                continue
        dates.add(record.as_of.astimezone(_IST).date())
    return dates


def _synthetic_vix_events(
    symbol: str,
    *,
    close: Decimal,
    bar_timestamp: datetime,
    as_of: datetime,
    normalization_version: str,
) -> tuple[CanonicalMarketEvent, CanonicalMarketEvent]:
    suffix = int(bar_timestamp.timestamp())
    quote = CanonicalMarketEvent(
        event_id=f"vix-backfill-{suffix}-quote",
        provider="fyers",
        symbol=symbol,
        event_type="QUOTE_SNAPSHOT",
        event_time=bar_timestamp,
        source_time=bar_timestamp,
        receive_time=as_of,
        provider_sequence=None,
        payload={
            "quotes": [
                {
                    "symbol": symbol,
                    "ltp": str(close),
                    "lp": str(close),
                }
            ]
        },
        raw_ref=f"vix-backfill/{suffix}",
        normalization_version=normalization_version,
    )
    bar = CanonicalMarketEvent(
        event_id=f"vix-backfill-{suffix}-bar",
        provider="fyers",
        symbol=symbol,
        event_type="BAR_SNAPSHOT",
        event_time=bar_timestamp,
        source_time=bar_timestamp,
        receive_time=as_of,
        provider_sequence=None,
        payload={
            "symbol": symbol,
            "resolution": _DAILY_RESOLUTION,
            "bar_count": 1,
            "bars": [
                {
                    "timestamp": int(bar_timestamp.timestamp()),
                    "open": str(close),
                    "high": str(close),
                    "low": str(close),
                    "close": str(close),
                    "volume": 0,
                }
            ],
            "is_final": True,
        },
        raw_ref=f"vix-backfill/{suffix}",
        normalization_version=normalization_version,
    )
    return quote, bar
