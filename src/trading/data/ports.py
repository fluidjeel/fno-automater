"""Typed ports for market data. Infrastructure implements these."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

from trading.data.events import CanonicalMarketEvent, RawMarketCapture
from trading.domain.contracts import FeatureSnapshot

__all__ = ["EventStore", "MarketFeedPort", "SnapshotBuilderPort"]


@runtime_checkable
class MarketFeedPort(Protocol):
    """Pull-based market data (Fyers REST in Layer 1)."""

    def fetch_option_chain(self, symbol: str) -> RawMarketCapture:
        """Return one option-chain snapshot for an underlying symbol."""
        ...

    def fetch_quotes(self, symbols: tuple[str, ...]) -> RawMarketCapture:
        """Return top-of-book quotes for the given symbols."""
        ...

    def fetch_history(
        self,
        symbol: str,
        *,
        resolution: str,
        range_from: str,
        range_to: str,
    ) -> RawMarketCapture:
        """Return OHLCV candles for one symbol and resolution."""
        ...

    def fetch_depth(self, symbol: str) -> RawMarketCapture:
        """Return a 5-level depth snapshot."""
        ...

    def fetch_market_status(self) -> RawMarketCapture:
        """Return exchange/segment status."""
        ...

    def fetch_expiry_dates(self, symbol: str) -> RawMarketCapture:
        """Return listed expiries when the provider supports it."""
        ...


@runtime_checkable
class EventStore(Protocol):
    """Append-only raw and canonical storage."""

    def append_raw(self, capture: RawMarketCapture) -> Path:
        """Persist raw payload; return the storage path used as raw_ref."""
        ...

    def append_canonical(self, event: CanonicalMarketEvent) -> None: ...

    def read_canonical(
        self,
        *,
        symbol: str,
        start: datetime,
        end: datetime,
    ) -> Sequence[CanonicalMarketEvent]: ...


@runtime_checkable
class SnapshotBuilderPort(Protocol):
    """Build a FeatureSnapshot from canonical events."""

    def build(
        self,
        events: Sequence[CanonicalMarketEvent],
        *,
        as_of: datetime,
    ) -> FeatureSnapshot: ...
