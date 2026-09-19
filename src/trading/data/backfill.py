"""Offline-capable backfill of instrument masters and historical bars."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, timedelta
from pathlib import Path

from trading.data.config import (
    DataPipelineConfig,
    ReferenceDataConfig,
    UnderlyingConfig,
)
from trading.data.fyers.symbol_master import (
    FyersSymbolMaster,
    parse_symbol_master,
)
from trading.data.normalize import normalize_fyers_history
from trading.data.ports import EventStore, MarketFeedPort
from trading.data.storage.catalog import CatalogWriter
from trading.data.storage.instrument_store import InstrumentSpecStore
from trading.domain.clock import Clock
from trading.domain.enums import Exchange

__all__ = [
    "HistoryBackfillResult",
    "InstrumentBackfillResult",
    "backfill_history",
    "backfill_instruments",
    "history_windows",
]

_SEGMENT_EXCHANGE = {
    "NSE_CM": Exchange.NSE,
    "NSE_FO": Exchange.NSE,
    "MCX_COM": Exchange.MCX,
}


@dataclass(frozen=True, slots=True)
class InstrumentBackfillResult:
    """Outcome of one instrument-master refresh."""

    segment: str
    spec_count: int
    raw_ref: str


@dataclass(frozen=True, slots=True)
class HistoryBackfillResult:
    """Outcome of one symbol/resolution history pull."""

    symbol: str
    resolution: str
    windows: tuple[tuple[date, date], ...]
    event_ids: tuple[str, ...]


def history_windows(
    *,
    end: date,
    days: int,
    max_days: int,
) -> tuple[tuple[date, date], ...]:
    """Split an inclusive lookback into UTC-aligned provider-legal chunks.

    Re-running the same (end, days, max_days) yields the same windows, so a
    retry produces the same request ranges and the catalog can dedupe on
    event_id.
    """
    if days < 1:
        raise ValueError("days must be at least 1")
    if max_days < 1:
        raise ValueError("history_max_days_by_resolution must be a positive window")
    start = end - timedelta(days=days - 1)
    windows: list[tuple[date, date]] = []
    cursor = start
    while cursor <= end:
        chunk_end = min(cursor + timedelta(days=max_days - 1), end)
        windows.append((cursor, chunk_end))
        cursor = chunk_end + timedelta(days=1)
    return tuple(windows)


def _max_days(reference: ReferenceDataConfig, resolution: str) -> int:
    """Fail closed when the per-request window has not been verified."""
    configured = reference.max_days_for(resolution)
    if configured is None:
        raise ValueError(
            f"resolution {resolution!r} has no history_max_days_by_resolution "
            "entry; an unverified window is not used"
        )
    return configured


def backfill_instruments(
    *,
    pipeline_config: DataPipelineConfig,
    repo_root: Path,
    store: EventStore,
    clock: Clock,
    fetcher: FyersSymbolMaster | None = None,
) -> tuple[InstrumentBackfillResult, ...]:
    """Download configured symbol masters and replace the instrument catalog."""
    reference = pipeline_config.reference
    master = fetcher or FyersSymbolMaster(
        clock,
        url_template=reference.symbol_master_url_template,
        timeout_seconds=reference.timeout_seconds,
    )
    catalog = InstrumentSpecStore(
        repo_root / pipeline_config.storage.root / reference.instrument_subdir
    )
    results: list[InstrumentBackfillResult] = []
    for segment in reference.segments:
        capture = master.fetch(segment)
        raw_path = store.append_raw(capture)
        raw_ref = str(raw_path.relative_to(repo_root))
        source = master.url_for(segment)
        specs = parse_symbol_master(
            capture,
            segment=segment,
            exchange=_SEGMENT_EXCHANGE.get(segment, Exchange.NSE),
            timezone=pipeline_config.session.timezone,
            index_instrument_type=reference.index_instrument_type,
            source=source,
        )
        catalog.write(segment, specs)
        results.append(
            InstrumentBackfillResult(
                segment=segment,
                spec_count=len(specs),
                raw_ref=raw_ref,
            )
        )
    return tuple(results)


def backfill_history(
    *,
    pipeline_config: DataPipelineConfig,
    repo_root: Path,
    feed: MarketFeedPort,
    store: EventStore,
    clock: Clock,
    underlying: UnderlyingConfig,
    resolutions: tuple[str, ...],
    days: int,
    catalog: CatalogWriter | None = None,
    utc_today: date | None = None,
) -> tuple[HistoryBackfillResult, ...]:
    """Pull chunked history and persist it as BAR_SNAPSHOT events."""
    end = utc_today if utc_today is not None else clock.now_utc().astimezone(UTC).date()
    version = pipeline_config.normalization_version
    results: list[HistoryBackfillResult] = []
    for resolution in resolutions:
        max_days = _max_days(pipeline_config.reference, resolution)
        windows = history_windows(end=end, days=days, max_days=max_days)
        event_ids: list[str] = []
        for start, stop in windows:
            capture = feed.fetch_history(
                underlying.symbol,
                resolution=resolution,
                range_from=start.isoformat(),
                range_to=stop.isoformat(),
            )
            raw_path = store.append_raw(capture)
            raw_ref = str(raw_path.relative_to(repo_root))
            event = normalize_fyers_history(
                capture,
                symbol=underlying.symbol,
                resolution=resolution,
                normalization_version=version,
                raw_ref=raw_ref,
            )
            store.append_canonical(event)
            if catalog is not None:
                catalog.append((event,))
            event_ids.append(event.event_id)
        results.append(
            HistoryBackfillResult(
                symbol=underlying.symbol,
                resolution=resolution,
                windows=windows,
                event_ids=tuple(event_ids),
            )
        )
    return tuple(results)
