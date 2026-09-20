"""Paper-only CAS depth collector process logic."""

from __future__ import annotations

import queue
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trading.data.cas_depth.adapter import CasDepthPaperAdapter
from trading.data.cas_depth.config import CasDataConfig
from trading.data.cas_depth.contracts import NormalizedDepthUpdate
from trading.data.cas_depth.metrics import CollectorMetrics
from trading.data.cas_depth.normalize import (
    normalize_data_ws_depth,
    normalize_tbt_depth,
)
from trading.data.cas_depth.quality import DepthQualityTracker
from trading.data.cas_depth.storage import CasDepthStorage, SnapshotRing
from trading.data.fyers.capability_probe import (
    check_tbt_entitlement,
    pick_liquid_option,
)
from trading.data.fyers.client import FyersMarketFeed
from trading.data.settings import FyersSettings

__all__ = ["CasDepthCollector", "CollectorRunResult", "resolve_subscription_symbols"]


@dataclass(frozen=True, slots=True)
class CollectorRunResult:
    """Outcome of one bounded collector run."""

    metrics: CollectorMetrics
    quality: dict[str, int]
    subscribed_symbols: tuple[str, ...]
    tbt_symbols: tuple[str, ...]
    mcx_symbols: tuple[str, ...]
    mcx_supported: bool
    stopped_reason: str
    report_path: Path | None = None


@dataclass
class _QueuedDepth:
    update: NormalizedDepthUpdate
    raw: dict[str, Any] | None = None


class CasDepthCollector:
    """Isolated CAS depth collector with bounded queue and batch writes."""

    def __init__(
        self,
        config: CasDataConfig,
        settings: FyersSettings,
        *,
        repo_root: Path,
        feed: FyersMarketFeed | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._config = config
        self._settings = settings
        self._repo_root = repo_root
        self._feed = feed
        self._sleep = sleep
        storage_root = repo_root / config.storage.root
        self._storage = CasDepthStorage(
            storage_root,
            sample_every_n=config.storage.sample_every_n,
        )
        self._adapter = CasDepthPaperAdapter(storage_root)
        self._metrics = CollectorMetrics()
        self._quality = DepthQualityTracker(
            max_stale_seconds=config.collector.max_stale_seconds,
        )
        self._ring = SnapshotRing(config.collector.max_snapshots_per_symbol)
        self._queue: queue.Queue[_QueuedDepth | None] = queue.Queue(
            maxsize=config.collector.queue_maxsize,
        )
        self._halt = threading.Event()
        self._last_publish: dict[str, float] = {}

    def run(
        self,
        *,
        duration_seconds: float,
        symbols: Sequence[str] | None = None,
    ) -> CollectorRunResult:
        """Run bounded collection for ``duration_seconds``."""
        if self._config.cas_live_orders:
            raise ValueError("cas_live_orders must remain false for paper depth collection")
        resolved = resolve_subscription_symbols(
            self._config,
            self._feed,
            override=symbols,
        )
        tbt_symbols = tuple(
            symbol for symbol in resolved if symbol.startswith(("NSE:", "NFO:"))
        )
        mcx_symbols = tuple(symbol for symbol in resolved if symbol.startswith("MCX:"))
        self._metrics.subscribed_symbols = resolved
        entitlement = check_tbt_entitlement(self._settings)
        stopped_reason = "duration"
        processor = threading.Thread(target=self._process_loop, daemon=True)
        processor.start()
        feeds: list[threading.Thread] = []
        data_ws_symbols: list[str] = list(mcx_symbols)
        if entitlement.get("entitled") and tbt_symbols:
            feeds.append(
                threading.Thread(
                    target=self._run_tbt_feed,
                    args=(tbt_symbols,),
                    daemon=True,
                )
            )
        else:
            if tbt_symbols:
                stopped_reason = "tbt_not_entitled"
            # DepthUpdate does not support index symbols; collect options/futures only.
            data_ws_symbols.extend(
                symbol for symbol in tbt_symbols if not symbol.endswith("-INDEX")
            )
        if data_ws_symbols:
            feeds.append(
                threading.Thread(
                    target=self._run_data_ws_feed,
                    args=(tuple(dict.fromkeys(data_ws_symbols)),),
                    daemon=True,
                )
            )
        for thread in feeds:
            thread.start()
        deadline = time.monotonic() + duration_seconds
        while time.monotonic() < deadline and not self._halt.is_set():
            self._metrics.sample_resources()
            self._metrics.record_queue_depth(self._queue.qsize())
            self._sleep(1.0)
        self._halt.set()
        self._queue.put(None)
        processor.join(timeout=5.0)
        for thread in feeds:
            thread.join(timeout=5.0)
        self._storage.flush_all()
        mcx_supported = any(
            symbol.startswith("MCX:")
            for symbol in resolved
            if self._metrics.messages_processed > 0
        )
        self._metrics.mcx_supported = mcx_supported
        quality = {
            "reconnects": self._quality.reconnects,
            "queue_overflows": self._quality.queue_overflows,
            "sequence_gaps": self._quality.sequence_gaps,
            "duplicates": self._quality.duplicates,
            "stale": self._quality.stale,
            "malformed": self._quality.malformed,
            "missing_fields": self._quality.missing_fields,
        }
        report_path = (
            self._repo_root
            / self._config.storage.root
            / self._config.benchmark.report_subdir
            / f"run_{datetime.now(tz=UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
        )
        self._metrics.write_report(report_path, quality=quality)
        return CollectorRunResult(
            metrics=self._metrics,
            quality=quality,
            subscribed_symbols=resolved,
            tbt_symbols=tbt_symbols,
            mcx_symbols=mcx_symbols,
            mcx_supported=mcx_supported,
            stopped_reason=stopped_reason,
            report_path=report_path,
        )

    def _process_loop(self) -> None:
        batch_count = 0
        while not self._halt.is_set():
            try:
                item = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if item is None:
                break
            started = time.monotonic()
            now = datetime.now(tz=UTC)
            issues = self._quality.inspect(item.update, now=now)
            update = item.update.model_copy(
                update={"quality_issues": issues},
            )
            self._metrics.messages_processed += 1
            self._metrics.record_latency(
                (time.monotonic() - started) * 1000.0,
            )
            for key in update.model_dump():
                self._metrics.fields_observed.add(key)
            prior = self._ring.push(update)
            self._storage.append_normalized(update)
            batch_count += 1
            if batch_count >= self._config.collector.batch_size:
                self._storage.flush_all()
                batch_count = 0
            self._maybe_publish(update, prior, now=now)

    def _maybe_publish(
        self,
        update: NormalizedDepthUpdate,
        prior: NormalizedDepthUpdate | None,
        *,
        now: datetime,
    ) -> None:
        last = self._last_publish.get(update.symbol, 0.0)
        if time.monotonic() - last < self._config.collector.snapshot_publish_interval_seconds:
            return
        snapshot = self._adapter.publish(
            update,
            prior,
            quality_issues=update.quality_issues,
            snapshot_id=f"cas-depth-{uuid.uuid4().hex[:12]}",
            now=now,
            max_stale_seconds=self._config.collector.max_stale_seconds,
        )
        self._storage.append_snapshot(snapshot)
        self._last_publish[update.symbol] = time.monotonic()

    def _enqueue(self, update: NormalizedDepthUpdate) -> None:
        self._metrics.messages_received += 1
        try:
            self._queue.put_nowait(_QueuedDepth(update=update))
        except queue.Full:
            self._quality.record_queue_overflow()
            self._metrics.messages_dropped += 1

    def _run_tbt_feed(self, symbols: Sequence[str]) -> None:
        try:
            from fyers_apiv3.FyersWebsocket.tbt_ws import (
                FyersTbtSocket,
                SubscriptionModes,
            )
        except ImportError:
            self._quality.record_reconnect()
            return

        channel = self._config.collector.tbt_channel
        connected = threading.Event()

        def on_depth_update(ticker: str, depth: Any) -> None:
            if self._halt.is_set():
                return
            receive_time = datetime.now(tz=UTC)
            normalized = normalize_tbt_depth(
                ticker,
                depth,
                receive_time=receive_time,
            )
            if normalized is not None:
                self._enqueue(normalized)

        def on_open() -> None:
            connected.set()
            socket.subscribe(set(symbols), channel, SubscriptionModes.DEPTH)
            socket.switchChannel({channel}, set())

        def on_error_message(message: str) -> None:
            if "reconnect" in message.lower():
                self._quality.record_reconnect()

        socket = FyersTbtSocket(
            access_token=self._settings.auth_header,
            write_to_file=False,
            on_depth_update=on_depth_update,
            on_error_message=on_error_message,
            on_open=on_open,
            reconnect=self._config.collector.reconnect_attempts > 1,
            reconnect_retry=self._config.collector.reconnect_attempts,
        )
        try:
            socket.connect()
            connected.wait(timeout=5.0)
            socket.keep_running()
            while not self._halt.is_set():
                self._sleep(0.2)
        finally:
            socket.stop_running()
            socket.close_connection()

    def _run_data_ws_feed(self, symbols: Sequence[str]) -> None:
        from fyers_apiv3.FyersWebsocket import data_ws

        def on_message(message: dict[str, Any]) -> None:
            if self._halt.is_set():
                return
            if message.get("s") == "error":
                if message.get("type") in {"cn", "AUTH"}:
                    self._halt.set()
                return
            if message.get("type") != "dp" and not message.get("bid_price1"):
                return
            receive_time = datetime.now(tz=UTC)
            normalized = normalize_data_ws_depth(
                message,
                receive_time=receive_time,
            )
            if normalized is not None:
                self._metrics.mcx_supported = True
                self._enqueue(normalized)

        log_dir = self._repo_root / "data" / "logs" / "cas_depth"
        log_dir.mkdir(parents=True, exist_ok=True)
        socket = data_ws.FyersDataSocket(
            access_token=self._settings.auth_header,
            write_to_file=False,
            log_path=str(log_dir),
            reconnect=False,
            on_message=on_message,
        )
        try:
            socket.connect()
            socket.subscribe(
                symbols=list(symbols),
                data_type="DepthUpdate",
                channel=self._config.collector.data_ws_channel,
            )
            socket.keep_running()
            while not self._halt.is_set():
                self._sleep(0.2)
        finally:
            socket.close_connection()


def resolve_subscription_symbols(
    config: CasDataConfig,
    feed: FyersMarketFeed | None,
    *,
    override: Sequence[str] | None = None,
) -> tuple[str, ...]:
    """Build the small configurable symbol list for CAS depth."""
    if override:
        return tuple(dict.fromkeys(override))
    symbols: list[str] = [config.symbols.index_or_future]
    if config.symbols.options:
        symbols.extend(config.symbols.options)
    elif feed is not None:
        symbols.extend(_resolve_options(feed, config))
    if feed is not None:
        for prefix in config.symbols.mcx:
            symbols.append(_resolve_mcx_symbol(feed, prefix))
    else:
        symbols.extend(config.symbols.mcx)
    return tuple(dict.fromkeys(symbols))


def _resolve_mcx_symbol(feed: FyersMarketFeed, prefix: str) -> str:
    for candidate in (prefix, f"{prefix}25OCTFUT", f"{prefix}25SEPFUT"):
        try:
            capture = feed.fetch_quotes([candidate])
        except Exception:
            continue
        rows = capture.payload.get("d", [])
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict) and row.get("s") == "ok":
                    symbol = row.get("n")
                    if isinstance(symbol, str):
                        return symbol
    return prefix


def _resolve_options(feed: FyersMarketFeed, config: CasDataConfig) -> list[str]:
    resolved: list[str] = []
    try:
        nifty = pick_liquid_option(feed.fetch_option_chain("NSE:NIFTY50-INDEX"))
        if nifty:
            resolved.append(nifty)
    except Exception:
        pass
    if config.symbols.max_options > 1:
        try:
            stock = pick_liquid_option(
                feed.fetch_option_chain(config.symbols.stock_option_underlying),
            )
            if stock:
                resolved.append(stock)
        except Exception:
            pass
    return resolved[: config.symbols.max_options]
