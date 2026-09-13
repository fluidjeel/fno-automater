"""Fyers REST client for Layer 1 market data."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from http import HTTPStatus
from typing import Any

import httpx

from trading.data.events import RawMarketCapture
from trading.data.settings import FyersSettings
from trading.domain.clock import Clock

__all__ = ["FyersApiError", "FyersMarketFeed"]


class FyersApiError(RuntimeError):
    """Raised when Fyers returns a non-success response."""


class FyersMarketFeed:
    """Pull quotes, bars, chain, depth and reference data via Fyers API v3."""

    def __init__(
        self,
        settings: FyersSettings,
        clock: Clock,
        *,
        strike_count: int = 25,
        chain_greeks: bool = True,
        history_oi_flag: bool = True,
    ) -> None:
        self._settings = settings
        self._clock = clock
        self._strike_count = strike_count
        self._chain_greeks = chain_greeks
        self._history_oi_flag = history_oi_flag

    def fetch_option_chain(self, symbol: str) -> RawMarketCapture:
        """Return one option-chain snapshot for an underlying symbol."""
        params: dict[str, str | int] = {
            "symbol": symbol,
            "strikecount": self._strike_count,
        }
        if self._chain_greeks:
            params["greeks"] = "1"
        return self._fetch(endpoint="options-chain-v3", params=params)

    def fetch_quotes(self, symbols: Sequence[str]) -> RawMarketCapture:
        """Return top-of-book quotes for up to 50 comma-separated symbols."""
        if not symbols:
            raise ValueError("fetch_quotes requires at least one symbol")
        return self._fetch(endpoint="quotes", params={"symbols": ",".join(symbols)})

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
        """Return OHLCV candles for one symbol and resolution."""
        flag = 1 if (self._history_oi_flag if oi_flag is None else oi_flag) else 0
        return self._fetch(
            endpoint="history",
            params={
                "symbol": symbol,
                "resolution": resolution,
                "date_format": date_format,
                "range_from": range_from,
                "range_to": range_to,
                "cont_flag": cont_flag,
                "oi_flag": flag,
            },
        )

    def fetch_depth(self, symbol: str, *, ohlcv_flag: int = 1) -> RawMarketCapture:
        """Return a 5-level depth snapshot for one symbol."""
        return self._fetch(
            endpoint="depth",
            params={"symbol": symbol, "ohlcv_flag": ohlcv_flag},
        )

    def fetch_market_status(self) -> RawMarketCapture:
        """Return exchange/segment market-status snapshot."""
        return self._fetch(endpoint="marketStatus", params={})

    def fetch_expiry_dates(self, symbol: str) -> RawMarketCapture:
        """Return listed expiries; may fail on some Fyers environments."""
        return self._fetch(endpoint="expiry", params={"symbol": symbol})

    def _fetch(
        self,
        *,
        endpoint: str,
        params: dict[str, str | int],
    ) -> RawMarketCapture:
        received_at = self._clock.now_utc()
        url = f"{self._settings.data_base_url}/{endpoint}"
        headers = {
            "Authorization": self._settings.auth_header,
            "Content-Type": "application/json",
            "version": "3",
        }
        with httpx.Client(timeout=30.0) as client:
            response = client.get(url, params=params, headers=headers)
        try:
            payload: dict[str, Any] = response.json()
        except json.JSONDecodeError as exc:
            raise FyersApiError(
                f"non-JSON response from Fyers ({response.status_code})"
            ) from exc
        if response.status_code != HTTPStatus.OK or payload.get("s") != "ok":
            message = payload.get("message", response.text)
            raise FyersApiError(f"Fyers error {response.status_code}: {message}")
        capture_id = hashlib.blake2b(
            json.dumps(payload, sort_keys=True).encode(),
            digest_size=8,
        ).hexdigest()
        return RawMarketCapture(
            capture_id=capture_id,
            provider="fyers",
            endpoint=endpoint,
            received_at=received_at,
            payload=payload,
            http_status=response.status_code,
        )
