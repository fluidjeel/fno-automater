"""Session instrument registry: catalog lookup with on-demand master resolution."""

from __future__ import annotations

from collections.abc import Callable, Mapping

from trading.data.config import ReferenceDataConfig
from trading.data.fyers.symbol_master import FyersSymbolMaster, parse_symbol_master
from trading.data.storage.instrument_store import InstrumentSpecStore
from trading.domain.clock import Clock
from trading.domain.contracts import InstrumentSpec
from trading.domain.enums import Exchange

__all__ = [
    "InstrumentRegistry",
    "SymbolMasterFetcher",
    "make_symbol_master_fetcher",
]

_SEGMENT_EXCHANGE = {
    "NSE_CM": Exchange.NSE,
    "NSE_FO": Exchange.NSE,
    "MCX_COM": Exchange.MCX,
}

SymbolMasterFetcher = Callable[[frozenset[str]], tuple[InstrumentSpec, ...]]


class InstrumentRegistry:
    """Resolve InstrumentSpec rows for chain and risk evaluation."""

    def __init__(
        self,
        store: InstrumentSpecStore,
        *,
        segment: str = "NSE_FO",
        fetch_master: SymbolMasterFetcher | None = None,
    ) -> None:
        self._store = store
        self._segment = segment
        self._fetch_master = fetch_master
        self._overlay: dict[str, InstrumentSpec] = {}

    def rebuild(self) -> int:
        """Reload the on-disk catalog after restart or master refresh."""
        self._overlay.clear()
        self._store.invalidate_cache()
        return len(self._store.list_all())

    def get(self, trading_symbol: str) -> InstrumentSpec | None:
        """Return one spec from overlay, catalog, or None."""
        if trading_symbol in self._overlay:
            return self._overlay[trading_symbol]
        spec = self._store.find(trading_symbol)
        if spec is not None:
            self._overlay[trading_symbol] = spec
        return spec

    def register(self, spec: InstrumentSpec) -> None:
        """Record one resolved spec for the remainder of the session."""
        self._overlay[spec.trading_symbol] = spec

    def ensure(
        self,
        symbols: frozenset[str],
        *,
        fetch_master: SymbolMasterFetcher | None = None,
    ) -> dict[str, InstrumentSpec]:
        """Resolve every symbol, fetching the master for any catalog miss."""
        fetcher = fetch_master or self._fetch_master
        resolved: dict[str, InstrumentSpec] = {}
        missing: set[str] = set()
        for symbol in symbols:
            if not symbol:
                continue
            spec = self.get(symbol)
            if spec is not None:
                resolved[symbol] = spec
            else:
                missing.add(symbol)
        if missing and fetcher is not None:
            for spec in fetcher(frozenset(missing)):
                self.register(spec)
                resolved[spec.trading_symbol] = spec
                self._store.merge(self._segment, (spec,))
        return resolved

    def as_mapping(self) -> Mapping[str, InstrumentSpec]:
        """Return overlay plus any catalog rows already touched this session."""
        merged = dict(self._overlay)
        for spec in self._store.list_all():
            merged.setdefault(spec.trading_symbol, spec)
        return merged

    def overlay_values(self) -> tuple[InstrumentSpec, ...]:
        """Specs resolved or registered during this session only."""
        return tuple(self._overlay.values())


def make_symbol_master_fetcher(
    *,
    clock: Clock,
    reference: ReferenceDataConfig,
    timezone: str,
    segment: str = "NSE_FO",
) -> SymbolMasterFetcher:
    """Build a filtered symbol-master fetcher for on-demand contract resolution."""

    master = FyersSymbolMaster(
        clock,
        url_template=reference.symbol_master_url_template,
        timeout_seconds=reference.timeout_seconds,
    )
    exchange = _SEGMENT_EXCHANGE.get(segment, Exchange.NSE)

    def fetch(symbols: frozenset[str]) -> tuple[InstrumentSpec, ...]:
        capture = master.fetch(segment)
        return parse_symbol_master(
            capture,
            segment=segment,
            exchange=exchange,
            timezone=timezone,
            index_instrument_type=reference.index_instrument_type,
            source=master.url_for(segment),
            symbols=symbols,
        )

    return fetch
