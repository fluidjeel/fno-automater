"""REST quote polling fallback for open-position protection."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta

from trading.domain.clock import Clock
from trading.domain.contracts.snapshot import MarketQuote
from trading.domain.enums import QuoteMonitorSource
from trading.runtime.quote_monitor import QuoteHandler

__all__ = ["RestQuoteMonitor", "RestQuoteRateLimitError"]

_logger = logging.getLogger(__name__)


class RestQuoteRateLimitError(Exception):
    """REST quote fetch hit provider rate limits."""


class RestQuoteMonitor:
    """Poll REST quotes for subscribed symbols."""

    def __init__(
        self,
        clock: Clock,
        fetch_quotes: Callable[[tuple[str, ...]], dict[str, MarketQuote]],
        *,
        poll_seconds: int = 2,
    ) -> None:
        self._clock = clock
        self._fetch = fetch_quotes
        self._poll_seconds = poll_seconds
        self._symbols: frozenset[str] = frozenset()
        self._handler: QuoteHandler | None = None
        self._running = False
        self._last_poll_at: datetime | None = None
        self._rate_limit_until: datetime | None = None
        self._rate_limit_backoff = poll_seconds
        self._pending_quotes: dict[str, MarketQuote] | None = None
        self._pending_index = 0

    def set_handler(self, handler: QuoteHandler) -> None:
        self._handler = handler

    def subscribe(self, symbols: frozenset[str]) -> None:
        self._symbols = symbols

    def start(self) -> None:
        self._running = True

    def stop(self) -> None:
        self._running = False
        self._pending_quotes = None
        self._pending_index = 0

    @property
    def ws_connected(self) -> bool:
        return False

    def tick(
        self,
        *,
        should_stop: Callable[[], bool] | None = None,
        carry_from: int = 0,
    ) -> int:
        """Fetch one REST snapshot and deliver quotes with optional carry-over."""
        if not self._running or not self._symbols or self._handler is None:
            return 0
        now = self._clock.now_utc()
        if self._rate_limit_until is not None and now < self._rate_limit_until:
            return carry_from
        if self._pending_quotes is None:
            if self._last_poll_at is not None:
                elapsed = now - self._last_poll_at
                if elapsed < timedelta(seconds=self._poll_seconds):
                    return carry_from
            self._last_poll_at = now
            try:
                quotes = self._fetch(tuple(sorted(self._symbols)))
            except RestQuoteRateLimitError:
                self._rate_limit_backoff = min(max(self._rate_limit_backoff * 2, 1), 60)
                self._rate_limit_until = now + timedelta(
                    seconds=self._rate_limit_backoff
                )
                return carry_from
            self._rate_limit_backoff = self._poll_seconds
            self._rate_limit_until = None
            self._pending_quotes = {
                symbol: quote
                for symbol, quote in quotes.items()
                if symbol in self._symbols
            }
            self._pending_index = carry_from
        received_at = self._clock.now_utc()
        symbols = tuple(sorted(self._pending_quotes))
        index = self._pending_index
        while index < len(symbols):
            if should_stop is not None and should_stop():
                self._pending_index = index
                return index
            symbol = symbols[index]
            quote = self._pending_quotes[symbol]
            self._handler(symbol, quote, QuoteMonitorSource.REST, received_at)
            index += 1
        self._pending_quotes = None
        self._pending_index = 0
        return 0

    def has_pending_delivery(self) -> bool:
        """Return whether a prior tick left undelivered REST quotes."""
        return self._pending_quotes is not None
