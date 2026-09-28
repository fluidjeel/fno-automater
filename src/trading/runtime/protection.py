"""Event-driven protection coordinator for open PAPER positions."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from trading.domain.clock import Clock
from trading.domain.contracts.protection import (
    ProtectionHeartbeat,
    SessionProtectionState,
)
from trading.domain.contracts.snapshot import MarketQuote
from trading.domain.enums import QuoteMonitorSource
from trading.runtime.fyers_ws_monitor import FyersWsQuoteMonitor
from trading.runtime.paper_runner import LifecycleAlert, PaperRunner, QuoteUpdateResult
from trading.runtime.quote_monitor import ScriptedQuoteMonitor
from trading.runtime.rest_quote_monitor import RestQuoteMonitor

__all__ = [
    "ProtectionConfig",
    "ProtectionCoordinator",
    "build_protection_coordinator",
]

_logger = logging.getLogger(__name__)


class ProtectionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = True
    ws_enabled: bool = True
    rest_poll_seconds: int = Field(default=2, ge=1)
    quote_max_age_ms: int = Field(default=5000, ge=1)
    heartbeat_interval_seconds: int = Field(default=5, ge=1)
    heartbeat_path: str = "data/paper/protection_heartbeat.json"
    watchdog_max_silence_seconds: int = Field(default=30, ge=1)
    deferred_work_floor_seconds: int = Field(default=7, ge=1)
    tick_budget_seconds: float = Field(default=2.0, ge=0.1)


@dataclass
class ProtectionCoordinator:
    """Subscribe to open-position symbols and drive fast exit evaluation."""

    runner: PaperRunner
    clock: Clock
    config: ProtectionConfig
    scripted: ScriptedQuoteMonitor
    rest: RestQuoteMonitor
    ws: FyersWsQuoteMonitor | None
    heartbeat_path: Path
    _pending_alerts: list[LifecycleAlert]
    _last_heartbeat_write: datetime | None
    _last_quote_at: datetime | None
    _dedupe: dict[str, tuple[str, int]]
    _quotes_dirty: bool
    _pending_m1: bool
    _last_deferred_work_at: datetime | None
    _rest_carry_index: int
    _last_m1_symbol: str | None
    _last_m1_quote: MarketQuote | None
    _last_m1_received_at: datetime | None
    _heartbeat_lock: threading.RLock

    def __init__(
        self,
        *,
        runner: PaperRunner,
        clock: Clock,
        config: ProtectionConfig,
        scripted: ScriptedQuoteMonitor,
        rest: RestQuoteMonitor,
        ws: FyersWsQuoteMonitor | None,
        heartbeat_path: Path,
    ) -> None:
        self.runner = runner
        self.clock = clock
        self.config = config
        self.scripted = scripted
        self.rest = rest
        self.ws = ws
        self.heartbeat_path = heartbeat_path
        self._pending_alerts = []
        self._last_heartbeat_write = None
        self._last_quote_at = None
        self._heartbeat_lock = threading.RLock()
        self._dedupe = {}
        self._quotes_dirty = False
        self._pending_m1 = False
        self._last_deferred_work_at = None
        self._rest_carry_index = 0
        self._last_m1_symbol = None
        self._last_m1_quote = None
        self._last_m1_received_at = None
        self._m1_quote_handler: (
            Callable[[str, MarketQuote, datetime], object | None] | None
        ) = None
        scripted.set_handler(self._on_quote)
        rest.set_handler(self._on_quote)
        if ws is not None:
            ws.set_handler(self._on_quote)

    def set_m1_quote_handler(
        self,
        handler: Callable[[str, MarketQuote, datetime], object | None] | None,
    ) -> None:
        """Register the production M1 ingress callback for provider quotes."""
        self._m1_quote_handler = handler

    @property
    def pending_alerts(self) -> tuple[LifecycleAlert, ...]:
        return tuple(self._pending_alerts)

    def drain_alerts(self) -> tuple[LifecycleAlert, ...]:
        alerts = tuple(self._pending_alerts)
        self._pending_alerts.clear()
        return alerts

    def start(self) -> None:
        symbols = self.runner.monitor_symbols()
        self.scripted.subscribe(symbols)
        self.rest.subscribe(symbols)
        if self.ws is not None:
            self.ws.subscribe(symbols)
            self.ws.start()
        self.scripted.start()
        self.rest.start()
        self._write_heartbeat(force=True)

    def stop(self) -> None:
        if self.ws is not None:
            self.ws.stop()
        self.rest.stop()
        self.scripted.stop()

    def refresh_subscriptions(self) -> None:
        symbols = self.runner.monitor_symbols()
        self.scripted.subscribe(symbols)
        self.rest.subscribe(symbols)
        if self.ws is not None:
            self.ws.subscribe(symbols)

    def tick(
        self,
        *,
        should_stop: Callable[[], bool] | None = None,
    ) -> QuoteUpdateResult | None:
        """REST fallback poll and heartbeat maintenance."""
        self._write_heartbeat()
        deadline = time.monotonic() + self.config.tick_budget_seconds

        def budget_exceeded() -> bool:
            if should_stop is not None and should_stop():
                return True
            return time.monotonic() >= deadline

        carry = self.rest.tick(
            should_stop=budget_exceeded,
            carry_from=self._rest_carry_index,
        )
        if carry > 0 or self.rest.has_pending_delivery():
            _logger.warning(
                "protection REST quote delivery exceeded %.1fs budget; "
                "carrying %s symbol(s) to next tick",
                self.config.tick_budget_seconds,
                carry,
            )
        self._rest_carry_index = carry
        return self._run_deferred_work()

    def publish_quotes(
        self,
        quotes: Mapping[str, MarketQuote],
        *,
        received_at: datetime,
        source: QuoteMonitorSource = QuoteMonitorSource.SCRIPTED,
    ) -> QuoteUpdateResult | None:
        """Synchronous quote injection for tests and positional sim."""
        filtered: dict[str, MarketQuote] = {}
        for symbol, quote in quotes.items():
            fingerprint = _fingerprint(quote)
            bucket = int(received_at.timestamp())
            prior = self._dedupe.get(symbol)
            if prior == (fingerprint, bucket) and not self.runner.protection_degraded:
                continue
            self._dedupe[symbol] = (fingerprint, bucket)
            filtered[symbol] = quote
        if not filtered:
            return None
        self._cache_quotes(filtered, source=source, received_at=received_at)
        self._last_quote_at = received_at
        for symbol, quote in filtered.items():
            self._last_m1_symbol = symbol
            self._last_m1_quote = quote
            self._last_m1_received_at = received_at
            self._pending_m1 = True
        return self._run_deferred_work(force=True)

    def seed_snapshots(self, snapshots: Mapping[str, object]) -> None:
        self.runner.seed_protection_snapshots(snapshots)  # type: ignore[arg-type]
        self.refresh_subscriptions()

    def _on_quote(
        self,
        symbol: str,
        quote: MarketQuote,
        source: QuoteMonitorSource,
        received_at: datetime,
    ) -> None:
        fingerprint = _fingerprint(quote)
        bucket = int(received_at.timestamp())
        prior = self._dedupe.get(symbol)
        if prior == (fingerprint, bucket) and not self.runner.protection_degraded:
            return
        self._dedupe[symbol] = (fingerprint, bucket)
        self._cache_quotes({symbol: quote}, source=source, received_at=received_at)
        self._last_quote_at = received_at
        self._last_m1_symbol = symbol
        self._last_m1_quote = quote
        self._last_m1_received_at = received_at
        self._pending_m1 = True
        self._write_heartbeat(force=True)

    def _cache_quotes(
        self,
        quotes: Mapping[str, MarketQuote],
        *,
        source: QuoteMonitorSource,
        received_at: datetime,
    ) -> None:
        self.runner.cache_quote_updates(
            quotes,
            source=source,
            received_at=received_at,
            quote_max_age_ms=self.config.quote_max_age_ms,
        )
        self._quotes_dirty = True

    def _run_deferred_work(self, *, force: bool = False) -> QuoteUpdateResult | None:
        now = self.clock.now_utc()
        if not force and self._last_deferred_work_at is not None:
            elapsed = now - self._last_deferred_work_at
            if elapsed < timedelta(seconds=self.config.deferred_work_floor_seconds):
                return None
        if not self._quotes_dirty and not self._pending_m1:
            return None
        before = self.runner.protection_degraded
        result: QuoteUpdateResult | None = None
        if self._quotes_dirty:
            events = self.runner.manage_exits(self.runner.protection_snapshots)
            self.runner.after_protection_quote_update(before)
            latency = 0 if self._last_quote_at is not None else None
            result = QuoteUpdateResult(
                events=events,
                degraded=self.runner.protection_degraded,
                recovered=before and not self.runner.protection_degraded,
                detection_latency_ms=latency,
            )
            self._quotes_dirty = False
            self.refresh_subscriptions()
            self._write_heartbeat(force=True)
        if (
            self._pending_m1
            and self._m1_quote_handler is not None
            and self._last_m1_symbol is not None
            and self._last_m1_quote is not None
            and self._last_m1_received_at is not None
        ):
            self._pending_m1 = False
            self._m1_quote_handler(
                self._last_m1_symbol,
                self._last_m1_quote,
                self._last_m1_received_at,
            )
        self._last_deferred_work_at = now
        return result

    def _maybe_write_heartbeat(self) -> None:
        now = self.clock.now_utc()
        if self._last_heartbeat_write is not None:
            elapsed = now - self._last_heartbeat_write
            if elapsed < timedelta(seconds=self.config.heartbeat_interval_seconds):
                return
        self._write_heartbeat()

    def _write_heartbeat(self, *, force: bool = False) -> None:
        """Snapshot and write the heartbeat atomically w.r.t. other threads.

        WS quote callbacks and the session thread both write the heartbeat;
        without serialisation a stale snapshot (``last_quote_at=None``) taken
        before a quote arrived can overwrite a fresher one.
        """
        with self._heartbeat_lock:
            now = self.clock.now_utc()
            if not force and self._last_heartbeat_write is not None:
                elapsed = now - self._last_heartbeat_write
                if elapsed < timedelta(seconds=self.config.heartbeat_interval_seconds):
                    return
            open_count = self.runner.open_position_count()
            heartbeat = ProtectionHeartbeat(
                as_of=now,
                open_positions=open_count,
                monitor_active=self.config.enabled,
                last_quote_at=self._last_quote_at,
                ws_connected=self.ws.ws_connected if self.ws is not None else False,
                protection_degraded=self.runner.protection_degraded,
            )
            self.heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
            self.heartbeat_path.write_text(
                heartbeat.model_dump_json(indent=2),
                encoding="utf-8",
            )
            session_state = SessionProtectionState(
                as_of=now,
                open_positions=open_count,
                monitor_active=self.config.enabled,
                ws_connected=heartbeat.ws_connected,
                protection_degraded=heartbeat.protection_degraded,
                last_quote_at=self._last_quote_at,
                monitor_symbols=tuple(sorted(self.runner.monitor_symbols())),
            )
            self.runner.persist_session_protection(session_state)
            self._last_heartbeat_write = now


def build_protection_coordinator(
    *,
    runner: PaperRunner,
    clock: Clock,
    config: ProtectionConfig,
    repo_root: Path,
    rest_fetch: RestQuoteMonitor | None = None,
    ws: FyersWsQuoteMonitor | None = None,
) -> ProtectionCoordinator:
    scripted = ScriptedQuoteMonitor()
    if rest_fetch is None:
        rest = RestQuoteMonitor(
            clock,
            lambda _symbols: {},
            poll_seconds=config.rest_poll_seconds,
        )
    else:
        rest = rest_fetch
    heartbeat_path = repo_root / config.heartbeat_path
    return ProtectionCoordinator(
        runner=runner,
        clock=clock,
        config=config,
        scripted=scripted,
        rest=rest,
        ws=ws,
        heartbeat_path=heartbeat_path,
    )


def _fingerprint(quote: MarketQuote) -> str:
    bid = quote.bid.value if quote.bid is not None else ""
    ask = quote.ask.value if quote.ask is not None else ""
    last = quote.last.value if quote.last is not None else ""
    return f"{bid}|{ask}|{last}"
