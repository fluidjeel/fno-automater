"""REST quote polling fallback for open-position protection."""

from __future__ import annotations

from collections.abc import Callable

from trading.domain.clock import Clock
from trading.domain.contracts.snapshot import MarketQuote
from trading.domain.enums import QuoteMonitorSource
from trading.runtime.quote_monitor import QuoteHandler

__all__ = ["RestQuoteMonitor"]


class RestQuoteMonitor:
    """Poll REST quotes for subscribed symbols."""

    def __init__(
        self,
        clock: Clock,
        fetch_quotes: Callable[[tuple[str, ...]], dict[str, MarketQuote]],
    ) -> None:
        self._clock = clock
        self._fetch = fetch_quotes
        self._symbols: frozenset[str] = frozenset()
        self._handler: QuoteHandler | None = None
        self._running = False

    def set_handler(self, handler: QuoteHandler) -> None:
        self._handler = handler

    def subscribe(self, symbols: frozenset[str]) -> None:
        self._symbols = symbols

    def start(self) -> None:
        self._running = True

    def stop(self) -> None:
        self._running = False

    @property
    def ws_connected(self) -> bool:
        return False

    def tick(self) -> None:
        """Fetch one REST snapshot for all subscribed symbols."""
        if not self._running or not self._symbols or self._handler is None:
            return
        received_at = self._clock.now_utc()
        quotes = self._fetch(tuple(sorted(self._symbols)))
        for symbol, quote in quotes.items():
            if symbol in self._symbols:
                self._handler(symbol, quote, QuoteMonitorSource.REST, received_at)
