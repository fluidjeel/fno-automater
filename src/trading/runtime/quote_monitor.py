"""Quote monitor ports for event-driven PAPER protection."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from trading.domain.contracts.snapshot import MarketQuote
from trading.domain.enums import QuoteMonitorSource

__all__ = [
    "QuoteHandler",
    "QuoteMonitorPort",
    "ScriptedQuoteMonitor",
    "quote_fingerprint",
]


QuoteHandler = Callable[[str, MarketQuote, QuoteMonitorSource, datetime], None]


class QuoteMonitorPort(Protocol):
    """Subscribe to symbols and deliver fresh quotes to a handler."""

    def subscribe(self, symbols: frozenset[str]) -> None: ...

    def start(self) -> None: ...

    def stop(self) -> None: ...

    @property
    def ws_connected(self) -> bool: ...


def quote_fingerprint(quote: MarketQuote) -> str:
    """Stable dedupe key for identical book observations."""
    bid = quote.bid.value if quote.bid is not None else ""
    ask = quote.ask.value if quote.ask is not None else ""
    last = quote.last.value if quote.last is not None else ""
    return f"{bid}|{ask}|{last}"


class ScriptedQuoteMonitor:
    """Test and sim monitor: quotes are published synchronously."""

    def __init__(self) -> None:
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

    def publish(
        self,
        symbol: str,
        quote: MarketQuote,
        *,
        source: QuoteMonitorSource = QuoteMonitorSource.SCRIPTED,
        received_at: datetime,
    ) -> None:
        """Inject one quote observation (tests and positional sim)."""
        if not self._running or self._handler is None:
            return
        if symbol not in self._symbols:
            return
        self._handler(symbol, quote, source, received_at)
