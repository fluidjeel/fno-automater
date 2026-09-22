"""Event-driven protection coordinator for open PAPER positions."""

from __future__ import annotations

from collections.abc import Mapping
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


class ProtectionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = True
    ws_enabled: bool = True
    rest_poll_seconds: int = Field(default=2, ge=1)
    quote_max_age_ms: int = Field(default=5000, ge=1)
    heartbeat_interval_seconds: int = Field(default=5, ge=1)
    heartbeat_path: str = "data/paper/protection_heartbeat.json"
    watchdog_max_silence_seconds: int = Field(default=30, ge=1)


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
        self._dedupe = {}
        scripted.set_handler(self._on_quote)
        rest.set_handler(self._on_quote)

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

    def tick(self) -> QuoteUpdateResult | None:
        """REST fallback poll and heartbeat maintenance."""
        self.rest.tick()
        self._maybe_write_heartbeat()
        return None

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
        result = self.runner.on_quote_update(
            filtered,
            source=source,
            received_at=received_at,
            quote_max_age_ms=self.config.quote_max_age_ms,
        )
        self._pending_alerts.extend(result.alerts)
        self._last_quote_at = received_at
        self.refresh_subscriptions()
        self._write_heartbeat(force=True)
        return result

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
        result = self.runner.on_quote_update(
            {symbol: quote},
            source=source,
            received_at=received_at,
            quote_max_age_ms=self.config.quote_max_age_ms,
        )
        self._pending_alerts.extend(result.alerts)
        self._last_quote_at = received_at
        self.refresh_subscriptions()

    def _maybe_write_heartbeat(self) -> None:
        now = self.clock.now_utc()
        if self._last_heartbeat_write is not None:
            elapsed = now - self._last_heartbeat_write
            if elapsed < timedelta(seconds=self.config.heartbeat_interval_seconds):
                return
        self._write_heartbeat()

    def _write_heartbeat(self, *, force: bool = False) -> None:
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
        rest = RestQuoteMonitor(clock, lambda _symbols: {})
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
