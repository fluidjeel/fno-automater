"""Orchestrate fetch → normalize → store → snapshot."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from trading.config import load_config
from trading.data.config import (
    DataPipelineConfig,
    UnderlyingConfig,
    load_data_pipeline_config,
)
from trading.data.cas_features import select_prior_depth
from trading.data.cycle import build_cycle_snapshot
from trading.data.events import CanonicalMarketEvent, RawMarketCapture
from trading.data.fyers.client import FyersApiError, FyersMarketFeed
from trading.data.macro_news import (
    JsonlMacroNewsFeed,
    MacroNewsFactor,
    merge_factor_into_payload,
    score_macro_news,
)
from trading.data.normalize import (
    normalize_fyers_depth,
    normalize_fyers_history,
    normalize_fyers_instrument_reference,
    normalize_fyers_market_status,
    normalize_fyers_option_chain,
    normalize_fyers_quotes,
)
from trading.data.ports import EventStore, MarketFeedPort
from trading.data.quality import assess_combined_snapshot
from trading.data.settings import FyersSettings
from trading.data.snapshot_builder import MarketSnapshotBuilder
from trading.data.storage.catalog import CatalogWriter
from trading.data.storage.instrument_store import InstrumentSpecStore
from trading.data.storage.snapshot_store import SnapshotRecord, SnapshotStore
from trading.data.storage.parquet_store import JsonlEventStore
from trading.domain.clock import Clock, WallClock
from trading.domain.contracts import FeatureSnapshot

__all__ = ["DataPipeline", "PipelineResult"]

_IST = ZoneInfo("Asia/Kolkata")


@dataclass(frozen=True, slots=True)
class PipelineResult:
    """Outcome of one pipeline cycle."""

    symbol: str
    events: tuple[CanonicalMarketEvent, ...]
    snapshot: FeatureSnapshot | None
    raw_refs: tuple[str, ...]
    macro_news_factor: MacroNewsFactor | None
    macro_news_parse_errors: tuple[str, ...] = ()


class DataPipeline:
    """One-shot or scheduled market-data ingestion."""

    def __init__(
        self,
        *,
        pipeline_config: DataPipelineConfig,
        feed: MarketFeedPort,
        store: EventStore,
        app_config_path: Path,
        repo_root: Path,
        clock: Clock,
        code_version: str = "0.1.0",
        catalog: CatalogWriter | None = None,
    ) -> None:
        self._pipeline_config = pipeline_config
        self._feed = feed
        self._store = store
        self._clock = clock
        self._catalog = catalog
        loaded = load_config(app_config_path)
        self._config_version = loaded.version
        self._config_checksum = loaded.checksum
        self._repo_root = repo_root
        self._code_version = code_version
        freshness = pipeline_config.freshness
        self._chain_max_age_ms = freshness.get("option_chain_max_age_ms", {}).get(
            "5m", 120_000
        )
        self._quote_max_age_ms = freshness.get("quote_max_age_ms", {}).get("5m", 60_000)
        self._bar_max_age_ms = freshness.get("bar_max_age_ms", {}).get("5m", 300_000)
        self._depth_max_age_ms = freshness.get("depth_max_age_ms", {}).get("5m", 60_000)
        self._macro_news_feed = JsonlMacroNewsFeed(
            repo_root / pipeline_config.macro_news.input_file
        )
        self._instruments = InstrumentSpecStore(
            repo_root
            / pipeline_config.storage.root
            / pipeline_config.reference.instrument_subdir
        )
        self._snapshots = SnapshotStore(
            repo_root
            / pipeline_config.storage.root
            / pipeline_config.storage.snapshot_subdir
        )

    def run_once(
        self,
        underlying: UnderlyingConfig,
        *,
        now: datetime | None = None,
    ) -> PipelineResult:
        symbol = underlying.symbol
        fyers = self._pipeline_config.fyers
        # None until `data backfill instruments` has run, which keeps tick and
        # lot size absent rather than guessed.
        spec = self._instruments.find(symbol)
        chain_capture = self._feed.fetch_option_chain(symbol)
        quote_capture = self._feed.fetch_quotes((symbol,))
        depth_capture: RawMarketCapture | None = None
        status_capture: RawMarketCapture | None = None
        expiry_capture: RawMarketCapture | None = None
        if fyers.fetch_depth:
            depth_capture = self._feed.fetch_depth(symbol)
        if fyers.fetch_market_status:
            status_capture = self._feed.fetch_market_status()
        if fyers.fetch_expiry_dates:
            try:
                expiry_capture = self._feed.fetch_expiry_dates(symbol)
            except FyersApiError:
                expiry_capture = None
        bar_captures: list[tuple[str, RawMarketCapture]] = []
        trade_date = chain_capture.received_at.astimezone(_IST).date()
        range_from = (trade_date - timedelta(days=fyers.bar_lookback_days)).isoformat()
        range_to = trade_date.isoformat()
        for resolution in fyers.bar_resolutions:
            bar_captures.append(
                (
                    resolution,
                    self._feed.fetch_history(
                        symbol,
                        resolution=resolution,
                        range_from=range_from,
                        range_to=range_to,
                    ),
                )
            )
        captures = [chain_capture, quote_capture, *(c for _, c in bar_captures)]
        if depth_capture is not None:
            captures.append(depth_capture)
        if status_capture is not None:
            captures.append(status_capture)
        if expiry_capture is not None:
            captures.append(expiry_capture)
        latest_receive = max(capture.received_at for capture in captures)
        instant = latest_receive if now is None else max(now, latest_receive)
        raw_refs: list[str] = []
        events: list[CanonicalMarketEvent] = []

        def persist(capture: RawMarketCapture) -> str:
            path = self._store.append_raw(capture)
            ref = str(path.relative_to(self._repo_root))
            raw_refs.append(ref)
            return ref

        chain_ref = persist(chain_capture)
        quote_ref = persist(quote_capture)
        version = self._pipeline_config.normalization_version
        chain_event = normalize_fyers_option_chain(
            chain_capture,
            symbol=symbol,
            normalization_version=version,
            raw_ref=chain_ref,
        )
        quote_event = normalize_fyers_quotes(
            quote_capture,
            symbol=symbol,
            normalization_version=version,
            raw_ref=quote_ref,
        )
        expiry_ref = (
            persist(expiry_capture) if expiry_capture is not None else chain_ref
        )
        ref_event = normalize_fyers_instrument_reference(
            chain_capture,
            symbol=symbol,
            normalization_version=version,
            raw_ref=expiry_ref if expiry_capture is not None else chain_ref,
            quote_capture=quote_capture,
            expiry_capture=expiry_capture,
            instrument_spec=spec,
        )
        news_items = self._macro_news_feed.items()
        load_result = self._macro_news_feed.last_load
        factor = score_macro_news(
            news_items,
            scope=underlying.underlying,
            as_of=instant,
            max_age_seconds=self._pipeline_config.macro_news.max_age_seconds,
            half_life_seconds=self._pipeline_config.macro_news.half_life_seconds,
            calculation_version=self._pipeline_config.macro_news.calculation_version,
        )
        parse_errors = load_result.errors if load_result is not None else ()
        chain_event = replace(
            chain_event,
            payload=merge_factor_into_payload(chain_event.payload, factor),
        )
        events.extend([chain_event, quote_event, ref_event])
        depth_event: CanonicalMarketEvent | None = None
        if depth_capture is not None:
            depth_event = normalize_fyers_depth(
                depth_capture,
                symbol=symbol,
                normalization_version=version,
                raw_ref=persist(depth_capture),
            )
            events.append(depth_event)
        status_event: CanonicalMarketEvent | None = None
        if status_capture is not None:
            status_event = normalize_fyers_market_status(
                status_capture,
                symbol=symbol,
                normalization_version=version,
                raw_ref=persist(status_capture),
                segment=self._pipeline_config.session.segment,
            )
            events.append(status_event)
        bar_event: CanonicalMarketEvent | None = None
        for resolution, capture in bar_captures:
            bar_event = normalize_fyers_history(
                capture,
                symbol=symbol,
                resolution=resolution,
                normalization_version=version,
                raw_ref=persist(capture),
            )
            events.append(bar_event)
        for event in events:
            self._store.append_canonical(event)
        if self._catalog is not None:
            self._catalog.append(events)
        snapshot_events = self._with_prior_depth(events, symbol=symbol, as_of=instant)
        quality = assess_combined_snapshot(
            chain=chain_event,
            quote=quote_event,
            bar=bar_event,
            now=instant,
            chain_max_age_ms=self._chain_max_age_ms,
            quote_max_age_ms=self._quote_max_age_ms,
            bar_max_age_ms=self._bar_max_age_ms,
            depth=depth_event,
            market_status=status_event,
            depth_max_age_ms=self._depth_max_age_ms,
            session=self._pipeline_config.session,
            quality_config=self._pipeline_config.quality,
        )
        builder = MarketSnapshotBuilder(
            underlying,
            config_version=self._config_version,
            config_checksum=self._config_checksum,
            code_version=self._code_version,
            greeks_calculation_version=fyers.greeks_calculation_version,
            instrument_spec=spec,
        )
        snapshot = build_cycle_snapshot(
            snapshot_events,
            quality=quality,
            as_of=instant,
            builder=builder,
        )
        record = SnapshotRecord.for_cycle(
            symbol=symbol,
            as_of=instant,
            quality=quality,
            event_ids=tuple(event.event_id for event in snapshot_events),
            snapshot=snapshot,
        )
        self._snapshots.append(record)
        if self._catalog is not None:
            self._catalog.append_snapshots([self._snapshots.index_rows(record)])
        return PipelineResult(
            symbol=symbol,
            events=tuple(events),
            snapshot=snapshot,
            raw_refs=tuple(raw_refs),
            macro_news_factor=factor,
            macro_news_parse_errors=parse_errors,
        )

    def _with_prior_depth(
        self,
        events: list[CanonicalMarketEvent],
        *,
        symbol: str,
        as_of: datetime,
    ) -> list[CanonicalMarketEvent]:
        """Reuse a recent stored depth snapshot. Missing stays missing."""
        current = next(
            (
                event
                for event in reversed(events)
                if event.event_type == "DEPTH_SNAPSHOT"
            ),
            None,
        )
        if current is None:
            return events
        lookback_ms = max(self._depth_max_age_ms, 1) * 2
        stored = self._store.read_canonical(
            symbol=symbol,
            start=as_of - timedelta(milliseconds=lookback_ms),
            end=as_of,
        )
        prior = select_prior_depth(stored, current=current)
        if prior is None:
            return events
        return [prior, *events]


def build_pipeline(repo_root: Path) -> DataPipeline:
    """Wire the default production pipeline from config on disk."""
    config_path = repo_root / "config" / "data_pipeline.yaml"
    pipeline_config = load_data_pipeline_config(config_path)
    settings = FyersSettings.from_repo_root(repo_root)
    cached = settings.load_cached_token(repo_root)
    if cached and not settings.fyers_access_token:
        settings = settings.model_copy(update={"fyers_access_token": cached})
    clock = WallClock()
    feed = FyersMarketFeed(
        settings,
        clock,
        strike_count=pipeline_config.fyers.option_chain_strike_count,
        chain_greeks=pipeline_config.fyers.chain_greeks,
        history_oi_flag=pipeline_config.fyers.history_oi_flag,
    )
    store_root = repo_root / pipeline_config.storage.root
    store: EventStore = JsonlEventStore(store_root)
    catalog = CatalogWriter(
        store_root,
        duckdb_path=repo_root / pipeline_config.storage.duckdb_path,
        parquet_subdir=pipeline_config.storage.parquet_subdir,
    )
    return DataPipeline(
        pipeline_config=pipeline_config,
        feed=feed,
        store=store,
        app_config_path=repo_root / "config" / "base.yaml",
        repo_root=repo_root,
        clock=clock,
        catalog=catalog,
    )
