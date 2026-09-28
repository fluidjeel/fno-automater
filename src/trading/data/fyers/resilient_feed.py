"""Rate-limited Fyers REST with TTL caches and transient-error backoff."""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from http import HTTPStatus

from trading.data.events import RawMarketCapture
from trading.data.fyers.client import FyersApiError, FyersMarketFeed, fyers_http_status
from trading.data.fyers.rate_limit import FyersRestRateLimiter
from trading.domain.clock import Clock

__all__ = ["ResilientFyersMarketFeed"]

_LOG = logging.getLogger(__name__)

_MARKET_STATUS_TTL_SECONDS = 60
_NEAR_CHAIN_TTL_SECONDS = 90
_SUPPLEMENTAL_CHAIN_TTL_SECONDS = 240
_HISTORY_TTL_SECONDS = 900
_BACKOFF_BASE_SECONDS = 30.0
_BACKOFF_MAX_SECONDS = 300.0
_BACKOFF_JITTER = 0.10
_RATE_LIMIT_WAIT = timedelta(seconds=15)
_RATE_LIMIT_ERROR = "Fyers error 429: request limit reached"


@dataclass(frozen=True, slots=True)
class _CachedCapture:
    capture: RawMarketCapture
    stored_at_mono: float


def _chain_key(symbol: str, expiry_epoch: int | None) -> tuple[str, int | None]:
    return symbol, expiry_epoch


def _history_key(
    symbol: str,
    resolution: str,
    range_from: str,
    range_to: str,
) -> tuple[str, str, str, str]:
    return symbol, resolution, range_from, range_to


def _is_transient_fyers_error(exc: FyersApiError) -> bool:
    status = fyers_http_status(exc)
    return status == HTTPStatus.TOO_MANY_REQUESTS or (
        status is not None and status >= HTTPStatus.INTERNAL_SERVER_ERROR
    )


class ResilientFyersMarketFeed:
    """Wrap Fyers REST with shared rate limits, caches and 429 backoff."""

    def __init__(
        self,
        inner: FyersMarketFeed,
        limiter: FyersRestRateLimiter,
        clock: Clock,
        *,
        rng: random.Random | None = None,
    ) -> None:
        self._inner = inner
        self._limiter = limiter
        self._clock = clock
        self._rng = rng or random.Random()  # noqa: S311
        self._market_status: _CachedCapture | None = None
        self._chains: dict[tuple[str, int | None], _CachedCapture] = {}
        self._history: dict[tuple[str, str, str, str], _CachedCapture] = {}
        self._backoff_until: datetime | None = None
        self._backoff_seconds = _BACKOFF_BASE_SECONDS

    @property
    def limiter(self) -> FyersRestRateLimiter:
        """Shared limiter for protection REST and chain calls."""
        return self._limiter

    def fetch_option_chain(
        self, symbol: str, *, expiry_epoch: int | None = None
    ) -> RawMarketCapture:
        key = _chain_key(symbol, expiry_epoch)
        ttl = (
            _NEAR_CHAIN_TTL_SECONDS
            if expiry_epoch is None
            else _SUPPLEMENTAL_CHAIN_TTL_SECONDS
        )
        cached = self._chains.get(key)
        if cached is not None and self._fresh(cached, ttl):
            return cached.capture
        if self._in_backoff() and cached is not None:
            _LOG.warning(
                "Fyers backoff active; serving cached option chain %s epoch=%s",
                symbol,
                expiry_epoch,
            )
            return cached.capture
        if not self._reserve_rest_call() and cached is not None:
            _LOG.warning(
                "Fyers REST rate cap; serving cached option chain %s epoch=%s",
                symbol,
                expiry_epoch,
            )
            return cached.capture
        try:
            capture = self._inner.fetch_option_chain(symbol, expiry_epoch=expiry_epoch)
        except FyersApiError as exc:
            if cached is not None and _is_transient_fyers_error(exc):
                self._enter_backoff(exc)
                _LOG.warning(
                    "option chain fetch failed for %s epoch=%s: %s; "
                    "cached chain retained",
                    symbol,
                    expiry_epoch,
                    exc,
                )
                return cached.capture
            raise
        self._chains[key] = _CachedCapture(
            capture=capture, stored_at_mono=time.monotonic()
        )
        self._clear_backoff()
        return capture

    def fetch_quotes(self, symbols: Sequence[str]) -> RawMarketCapture:
        if not self._reserve_rest_call():
            raise FyersApiError(_RATE_LIMIT_ERROR)
        return self._inner.fetch_quotes(symbols)

    def fetch_history(
        self,
        symbol: str,
        *,
        resolution: str,
        range_from: str,
        range_to: str,
        date_format: int = 1,
        cont_flag: int = 0,
        oi_flag: int | None = None,
    ) -> RawMarketCapture:
        key = _history_key(symbol, resolution, range_from, range_to)
        cached = self._history.get(key)
        if cached is not None and self._fresh(cached, _HISTORY_TTL_SECONDS):
            return cached.capture
        if not self._reserve_rest_call():
            if cached is not None:
                _LOG.warning(
                    "Fyers REST rate cap; serving cached history %s res=%s",
                    symbol,
                    resolution,
                )
                return cached.capture
            raise FyersApiError(_RATE_LIMIT_ERROR)
        try:
            capture = self._inner.fetch_history(
                symbol,
                resolution=resolution,
                range_from=range_from,
                range_to=range_to,
                date_format=date_format,
                cont_flag=cont_flag,
                oi_flag=oi_flag,
            )
        except FyersApiError as exc:
            if cached is not None and _is_transient_fyers_error(exc):
                self._enter_backoff(exc)
                _LOG.warning(
                    "history fetch failed for %s res=%s: %s; cached history retained",
                    symbol,
                    resolution,
                    exc,
                )
                return cached.capture
            raise
        self._history[key] = _CachedCapture(
            capture=capture, stored_at_mono=time.monotonic()
        )
        self._clear_backoff()
        return capture

    def fetch_depth(self, symbol: str, *, ohlcv_flag: int = 1) -> RawMarketCapture:
        if not self._reserve_rest_call():
            raise FyersApiError(_RATE_LIMIT_ERROR)
        return self._inner.fetch_depth(symbol, ohlcv_flag=ohlcv_flag)

    def fetch_market_status(self) -> RawMarketCapture:
        cached = self._market_status
        if cached is not None and self._fresh(cached, _MARKET_STATUS_TTL_SECONDS):
            return cached.capture
        if self._in_backoff() and cached is not None:
            _LOG.warning("Fyers backoff active; serving cached market status")
            return cached.capture
        if not self._reserve_rest_call() and cached is not None:
            _LOG.warning("Fyers REST rate cap; serving cached market status")
            return cached.capture
        try:
            capture = self._inner.fetch_market_status()
        except FyersApiError as exc:
            if cached is not None and _is_transient_fyers_error(exc):
                self._enter_backoff(exc)
                _LOG.warning(
                    "market status fetch failed: %s; last known status retained",
                    exc,
                )
                return cached.capture
            raise
        self._market_status = _CachedCapture(
            capture=capture, stored_at_mono=time.monotonic()
        )
        self._clear_backoff()
        return capture

    def fetch_expiry_dates(self, symbol: str) -> RawMarketCapture:
        if not self._reserve_rest_call():
            raise FyersApiError(_RATE_LIMIT_ERROR)
        return self._inner.fetch_expiry_dates(symbol)

    def _reserve_rest_call(self) -> bool:
        """Wait up to 15s for a REST token before giving up."""
        return self._limiter.acquire_or_wait(max_wait=_RATE_LIMIT_WAIT)

    def _fresh(self, entry: _CachedCapture, ttl_seconds: float) -> bool:
        return time.monotonic() - entry.stored_at_mono < ttl_seconds

    def _in_backoff(self) -> bool:
        if self._backoff_until is None:
            return False
        return self._clock.now_utc() < self._backoff_until

    def _enter_backoff(self, exc: FyersApiError) -> None:
        jitter = 1.0 + self._rng.uniform(-_BACKOFF_JITTER, _BACKOFF_JITTER)
        if fyers_http_status(exc) == HTTPStatus.TOO_MANY_REQUESTS:
            self._backoff_seconds = min(
                max(self._backoff_seconds * 2.0, _BACKOFF_BASE_SECONDS),
                _BACKOFF_MAX_SECONDS,
            )
        else:
            self._backoff_seconds = _BACKOFF_BASE_SECONDS
        delay = self._backoff_seconds * jitter
        self._backoff_until = self._clock.now_utc() + timedelta(seconds=delay)
        _LOG.warning(
            "Fyers transient error; backing off %.1fs until %s",
            delay,
            self._backoff_until.isoformat(),
        )

    def _clear_backoff(self) -> None:
        self._backoff_until = None
        self._backoff_seconds = _BACKOFF_BASE_SECONDS
