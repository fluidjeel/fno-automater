"""Fyers websocket quote monitor for open PAPER positions."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Sequence
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from trading.data.events import CanonicalMarketEvent, RawMarketCapture
from trading.data.fyers.ws import FyersTickStream
from trading.data.settings import FyersSettings
from trading.domain.clock import Clock
from trading.domain.contracts.snapshot import MarketQuote
from trading.domain.primitives import Price, TickSize
from trading.runtime.quote_monitor import QuoteHandler

__all__ = ["FyersWsQuoteMonitor"]


class _NullEventStore:
    def append_raw(self, _capture: RawMarketCapture) -> Path:
        return Path("/dev/null")

    def append_canonical(self, _event: CanonicalMarketEvent) -> None:
        return None

    def read_canonical(
        self,
        *,
        symbol: str,
        start: datetime,
        end: datetime,
    ) -> Sequence[CanonicalMarketEvent]:
        return ()


class FyersWsQuoteMonitor:
    """Background websocket subscriber for protection quotes."""

    def __init__(
        self,
        settings: FyersSettings,
        clock: Clock,
        repo_root: Path,
        *,
        tick_handler: Callable[[str, dict[str, object], datetime], MarketQuote | None]
        | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        self._settings = settings
        self._clock = clock
        self._repo_root = repo_root
        self._tick_handler = tick_handler or _default_tick_quote
        self._sleeper = sleeper
        self._symbols: frozenset[str] = frozenset()
        self._handler: QuoteHandler | None = None
        self._running = False
        self._connected = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def set_handler(self, handler: QuoteHandler) -> None:
        self._handler = handler

    def subscribe(self, symbols: frozenset[str]) -> None:
        self._symbols = symbols

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        self._connected = False

    @property
    def ws_connected(self) -> bool:
        return self._connected

    def _run(self) -> None:
        sleeper = self._sleeper or time.sleep
        store = _NullEventStore()
        while self._running and not self._stop.is_set():
            if not self._symbols:
                sleeper(0.5)
                continue
            stream = FyersTickStream(
                self._settings,
                self._clock,
                store,
                repo_root=self._repo_root,
                normalization_version="1",
                reconnect=True,
                reconnect_attempts=2,
                sleep=sleeper,
            )
            symbol = next(iter(sorted(self._symbols)))
            self._connected = True
            try:
                stream.collect(
                    symbol,
                    max_ticks=1,
                    duration_seconds=1,
                    stop=self._stop,
                )
            except (OSError, RuntimeError):
                self._connected = False
            sleeper(0.2)


def _default_tick_quote(
    _symbol: str, message: dict[str, object], _received_at: datetime
) -> MarketQuote | None:
    last = message.get("ltp", message.get("lp"))
    bid = message.get("bid")
    ask = message.get("ask")
    if last is None and bid is None and ask is None:
        return None
    tick = TickSize.of(Decimal("0.05"))
    anchor = last if last is not None else bid
    if anchor is None:
        return None
    last_price = Price.snap(str(anchor), tick)
    bid_price = Price.snap(str(bid), tick) if bid is not None else None
    ask_price = Price.snap(str(ask), tick) if ask is not None else None
    return MarketQuote(
        last=last_price,
        bid=bid_price,
        ask=ask_price,
    )
