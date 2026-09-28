"""Attach promoted-strike depth to option candidates for the PAPER session."""

from __future__ import annotations

from typing import Protocol

from trading.config.depth_promotion import DepthPromotionConfig
from trading.data.events import RawMarketCapture
from trading.data.fyers.client import FyersApiError
from trading.data.normalize import normalize_fyers_depth
from trading.data.paper_chain_cache import ChainCacheHealth, is_fyers_rate_limited
from trading.data.prices import depth_top_sizes
from trading.data.promoted_depth_ws import PromotedDepthWebSocket
from trading.domain.contracts import FeatureSnapshot
from trading.domain.enums import DepthFeedSource
from trading.identification.p1_features import top_book_size

__all__ = [
    "DepthAttachmentHealth",
    "attach_promoted_depth",
]


class _DepthFeed(Protocol):
    def fetch_depth(self, symbol: str) -> RawMarketCapture: ...


class DepthAttachmentHealth:
    """Per-poll depth source telemetry."""

    def __init__(self) -> None:
        self.sources: dict[str, DepthFeedSource] = {}
        self.rest_fallback_count = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "sources": {
                symbol: source.value for symbol, source in self.sources.items()
            },
            "rest_fallback_count": self.rest_fallback_count,
        }


def attach_promoted_depth(
    candidates: tuple[FeatureSnapshot, ...],
    *,
    promoted: frozenset[str],
    depth_ws: PromotedDepthWebSocket | None,
    feed: _DepthFeed,
    config: DepthPromotionConfig,
    chain_health: ChainCacheHealth,
) -> tuple[tuple[FeatureSnapshot, ...], DepthAttachmentHealth]:
    """Apply WS depth for promoted strikes; REST only as a bounded fallback."""
    health = DepthAttachmentHealth()
    if not candidates or not promoted:
        return candidates, health
    by_symbol = {item.contract.symbol: item for item in candidates}
    updated = dict(by_symbol)
    allow_rest = (
        config.rest_depth_fallback_enabled
        and chain_health.fetch_mode.value != "BACKOFF"
    )
    for symbol in sorted(promoted):
        item = by_symbol.get(symbol)
        if item is None:
            continue
        if top_book_size(item) is not None:
            health.sources[symbol] = DepthFeedSource.WEBSOCKET
            continue
        ws_book = depth_ws.snapshot(symbol) if depth_ws is not None else None
        if ws_book is not None:
            quote = item.market.model_copy(
                update={"bid_size": ws_book.bid_size, "ask_size": ws_book.ask_size}
            )
            updated[symbol] = item.model_copy(update={"market": quote})
            health.sources[symbol] = DepthFeedSource.WEBSOCKET
            continue
        if not allow_rest or depth_ws is None or not depth_ws.ws_connected:
            health.sources[symbol] = DepthFeedSource.UNAVAILABLE
            continue
        try:
            capture = feed.fetch_depth(symbol)
        except (FyersApiError, ValueError, OSError) as exc:
            if is_fyers_rate_limited(exc):
                health.sources[symbol] = DepthFeedSource.UNAVAILABLE
                continue
            health.sources[symbol] = DepthFeedSource.UNAVAILABLE
            continue
        event = normalize_fyers_depth(
            capture,
            symbol=symbol,
            normalization_version="1",
            raw_ref=capture.capture_id,
        )
        bid_size, ask_size = depth_top_sizes(event)
        if bid_size is None or ask_size is None:
            health.sources[symbol] = DepthFeedSource.UNAVAILABLE
            continue
        quote = item.market.model_copy(
            update={"bid_size": bid_size, "ask_size": ask_size}
        )
        updated[symbol] = item.model_copy(update={"market": quote})
        health.sources[symbol] = DepthFeedSource.REST_FALLBACK
        health.rest_fallback_count += 1
    return tuple(updated[item.contract.symbol] for item in candidates), health
