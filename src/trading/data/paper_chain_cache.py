"""TTL-cached option-chain fetch with 429 backoff for the PAPER session."""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from trading.config.depth_promotion import DepthPromotionConfig, RateLimitBackoffConfig
from trading.data.events import RawMarketCapture
from trading.data.fyers.client import FyersApiError
from trading.domain.clock import Clock
from trading.domain.enums import ChainFetchMode

__all__ = [
    "CachedOptionChainFeed",
    "ChainCacheEntry",
    "ChainCacheHealth",
    "ChainCachingMarketFeed",
    "is_fyers_rate_limited",
]


def is_fyers_rate_limited(exc: BaseException) -> bool:
    """True when a Fyers REST error indicates HTTP 429."""
    return isinstance(exc, FyersApiError) and "429" in str(exc)


@dataclass(frozen=True, slots=True)
class ChainCacheEntry:
    """One cached option-chain response."""

    capture: RawMarketCapture
    fetched_at: datetime
    symbol: str
    expiry_epoch: int | None


@dataclass(frozen=True, slots=True)
class ChainCacheHealth:
    """Rate-limit and cache telemetry for session health output."""

    fetch_mode: ChainFetchMode
    rate_limit_429_count: int
    current_backoff_seconds: float
    last_429_at: datetime | None
    cache_age_seconds: float | None

    def as_dict(self) -> dict[str, object]:
        return {
            "fetch_mode": self.fetch_mode.value,
            "rate_limit_429_count": self.rate_limit_429_count,
            "current_backoff_seconds": self.current_backoff_seconds,
            "last_429_at": (
                None if self.last_429_at is None else self.last_429_at.isoformat()
            ),
            "cache_age_seconds": self.cache_age_seconds,
        }


class _OptionChainSource(Protocol):
    def fetch_option_chain(
        self, symbol: str, *, expiry_epoch: int | None = None
    ) -> RawMarketCapture: ...


class _ChainFeed(_OptionChainSource, Protocol):
    def fetch_quotes(self, symbols: tuple[str, ...]) -> RawMarketCapture: ...

    def fetch_history(
        self,
        symbol: str,
        *,
        resolution: str,
        range_from: str,
        range_to: str,
    ) -> RawMarketCapture: ...

    def fetch_depth(self, symbol: str) -> RawMarketCapture: ...

    def fetch_market_status(self) -> RawMarketCapture: ...

    def fetch_expiry_dates(self, symbol: str) -> RawMarketCapture: ...


class ChainCachingMarketFeed:
    """Market feed that caches only ``fetch_option_chain`` for the PAPER session."""

    def __init__(self, inner: _ChainFeed, cache: CachedOptionChainFeed) -> None:
        self._inner = inner
        self._cache = cache
        self._last_health: ChainCacheHealth | None = None

    @property
    def last_chain_health(self) -> ChainCacheHealth | None:
        return self._last_health

    @property
    def cache(self) -> CachedOptionChainFeed:
        return self._cache

    def fetch_option_chain(
        self, symbol: str, *, expiry_epoch: int | None = None
    ) -> RawMarketCapture:
        capture, health = self._cache.fetch_option_chain(
            symbol, expiry_epoch=expiry_epoch
        )
        self._last_health = health
        return capture

    def fetch_quotes(self, symbols: tuple[str, ...]) -> RawMarketCapture:
        return self._inner.fetch_quotes(symbols)

    def fetch_history(
        self,
        symbol: str,
        *,
        resolution: str,
        range_from: str,
        range_to: str,
    ) -> RawMarketCapture:
        return self._inner.fetch_history(
            symbol,
            resolution=resolution,
            range_from=range_from,
            range_to=range_to,
        )

    def fetch_depth(self, symbol: str) -> RawMarketCapture:
        return self._inner.fetch_depth(symbol)

    def fetch_market_status(self) -> RawMarketCapture:
        return self._inner.fetch_market_status()

    def fetch_expiry_dates(self, symbol: str) -> RawMarketCapture:
        return self._inner.fetch_expiry_dates(symbol)


class CachedOptionChainFeed:
    """Serve one wide chain snapshot per TTL; back off and cache on 429."""

    def __init__(
        self,
        feed: _OptionChainSource,
        config: DepthPromotionConfig,
        clock: Clock,
        *,
        jitter_rng: random.Random | None = None,
    ) -> None:
        self._feed = feed
        self._config = config
        self._clock = clock
        self._rng = jitter_rng or random.Random()  # noqa: S311
        self._entries: dict[tuple[str, int | None], ChainCacheEntry] = {}
        self._backoff_until: datetime | None = None
        self._consecutive_429s = 0
        self._rate_limit_429_count = 0
        self._last_429_at: datetime | None = None

    @property
    def health(self) -> ChainCacheHealth:
        return self._health(ChainFetchMode.CACHED, cache_age_seconds=None)

    def fetch_option_chain(
        self,
        symbol: str,
        *,
        expiry_epoch: int | None = None,
        now: datetime | None = None,
    ) -> tuple[RawMarketCapture, ChainCacheHealth]:
        """Return a chain capture and health telemetry for this poll."""
        instant = now or self._clock.now_utc()
        key = (symbol, expiry_epoch)
        cached = self._entries.get(key)
        cache_age = (
            None
            if cached is None
            else max(0.0, (instant - cached.fetched_at).total_seconds())
        )
        ttl = timedelta(seconds=self._config.chain_cache_ttl_seconds)
        if (
            cached is not None
            and cache_age is not None
            and cache_age < ttl.total_seconds()
        ):
            return cached.capture, self._health(ChainFetchMode.CACHED, cache_age)

        if self._backoff_until is not None and instant < self._backoff_until:
            if cached is None:
                raise FyersApiError(
                    "option chain unavailable during rate-limit backoff "
                    "with empty cache"
                )
            return cached.capture, self._health(ChainFetchMode.BACKOFF, cache_age)

        try:
            capture = self._feed.fetch_option_chain(symbol, expiry_epoch=expiry_epoch)
        except FyersApiError as exc:
            if not is_fyers_rate_limited(exc):
                raise
            self._on_rate_limited(instant)
            if cached is None:
                raise
            cache_age = max(0.0, (instant - cached.fetched_at).total_seconds())
            return cached.capture, self._health(ChainFetchMode.BACKOFF, cache_age)

        self._consecutive_429s = 0
        self._backoff_until = None
        self._entries[key] = ChainCacheEntry(
            capture=capture,
            fetched_at=instant,
            symbol=symbol,
            expiry_epoch=expiry_epoch,
        )
        return capture, self._health(ChainFetchMode.LIVE, 0.0)

    def _on_rate_limited(self, now: datetime) -> None:
        self._rate_limit_429_count += 1
        self._last_429_at = now
        self._consecutive_429s += 1
        delay = self._backoff_delay(
            self._config.rate_limit_backoff, self._consecutive_429s
        )
        self._backoff_until = now + timedelta(seconds=delay)

    def _backoff_delay(self, cfg: RateLimitBackoffConfig, attempts: int) -> float:
        base = float(cfg.base_seconds)
        cap = float(cfg.cap_seconds)
        raw = min(cap, base * (2 ** max(0, attempts - 1)))
        jitter = float(cfg.jitter_fraction)
        if jitter <= 0:
            return float(raw)
        factor = 1.0 + self._rng.uniform(-jitter, jitter)
        return float(max(0.0, raw * factor))

    def _health(
        self,
        mode: ChainFetchMode,
        cache_age_seconds: float | None,
    ) -> ChainCacheHealth:
        now = self._clock.now_utc()
        backoff = 0.0
        if self._backoff_until is not None and now < self._backoff_until:
            backoff = max(0.0, (self._backoff_until - now).total_seconds())
        return ChainCacheHealth(
            fetch_mode=mode,
            rate_limit_429_count=self._rate_limit_429_count,
            current_backoff_seconds=backoff,
            last_429_at=self._last_429_at,
            cache_age_seconds=cache_age_seconds,
        )
