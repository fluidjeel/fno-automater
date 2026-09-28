"""Rolling order-flow imbalance (Cont, Kukanov and Stoikov) for M1.

The tracker is an accumulator: the runtime feeds it quotes with their exchange
timestamps and asks for statistics at an injected instant.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from trading.forecast.inputs import OrderFlowStats

__all__ = ["OrderFlowTracker", "Quote", "microprice", "ofi_increment"]

_ZERO = Decimal(0)
_TWO = Decimal(2)
_BPS = Decimal(10000)
_MAX_AGE = timedelta(minutes=30)


@dataclass(frozen=True, slots=True)
class Quote:
    """Top-of-book observation."""

    at: datetime
    bid: Decimal
    bid_size: Decimal
    ask: Decimal
    ask_size: Decimal


def ofi_increment(previous: Quote, current: Quote) -> Decimal:
    """Signed order-flow contribution between two consecutive quotes."""
    flow = _ZERO
    if current.bid >= previous.bid:
        flow += current.bid_size
    if current.bid <= previous.bid:
        flow -= previous.bid_size
    if current.ask <= previous.ask:
        flow -= current.ask_size
    if current.ask >= previous.ask:
        flow += previous.ask_size
    return flow


def microprice(quote: Quote) -> Decimal:
    """Size-weighted mid: leans toward the side with less resting size."""
    depth = quote.bid_size + quote.ask_size
    if depth <= 0:
        return (quote.bid + quote.ask) / _TWO
    return (quote.ask * quote.bid_size + quote.bid * quote.ask_size) / depth


class OrderFlowTracker:
    """Per-role quote history; roles are ``CALL`` and ``PUT`` near the money."""

    def __init__(self, *, short_seconds: int, long_seconds: int) -> None:
        self._short = timedelta(seconds=short_seconds)
        self._long = timedelta(seconds=long_seconds)
        self._history: dict[str, deque[Quote]] = {}

    def observe(self, role: str, quote: Quote) -> None:
        """Append a quote; duplicates and out-of-order stamps are ignored."""
        if quote.bid <= 0 or quote.ask <= quote.bid:
            return
        history = self._history.setdefault(role, deque())
        if history and quote.at <= history[-1].at:
            return
        history.append(quote)
        while history and quote.at - history[0].at > _MAX_AGE:
            history.popleft()

    def _window(self, role: str, now: datetime, span: timedelta) -> list[Quote]:
        return [item for item in self._history.get(role, ()) if now - item.at <= span]

    def _normalized_ofi(
        self, role: str, now: datetime, span: timedelta
    ) -> Decimal | None:
        window = self._window(role, now, span)
        if len(window) < 2:  # noqa: PLR2004 - a flow needs two quotes
            return None
        flow = sum(
            (ofi_increment(window[i - 1], window[i]) for i in range(1, len(window))),
            _ZERO,
        )
        depth = sum(
            ((q.bid_size + q.ask_size) / _TWO for q in window), _ZERO
        ) / Decimal(len(window))
        return None if depth <= 0 else flow / depth

    def _drift_bps(self, role: str, now: datetime) -> Decimal | None:
        window = self._window(role, now, self._long)
        if len(window) < 2:  # noqa: PLR2004 - drift needs two quotes
            return None
        first, last = microprice(window[0]), microprice(window[-1])
        mid = (window[-1].bid + window[-1].ask) / _TWO
        return None if mid <= 0 else (last - first) / mid * _BPS

    def stats(self, now: datetime) -> OrderFlowStats:
        """Call flow minus put flow; either side missing leaves the other alone."""

        def combine(call: Decimal | None, put: Decimal | None) -> Decimal | None:
            if call is None and put is None:
                return None
            return (call or _ZERO) - (put or _ZERO)

        samples = sum(len(self._window(r, now, self._long)) for r in ("CALL", "PUT"))
        return OrderFlowStats(
            ofi_short=combine(
                self._normalized_ofi("CALL", now, self._short),
                self._normalized_ofi("PUT", now, self._short),
            ),
            ofi_long=combine(
                self._normalized_ofi("CALL", now, self._long),
                self._normalized_ofi("PUT", now, self._long),
            ),
            microprice_drift_bps=combine(
                self._drift_bps("CALL", now), self._drift_bps("PUT", now)
            ),
            samples=samples,
        )
