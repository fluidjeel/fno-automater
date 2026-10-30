"""Mock REST market feed for integration tests (implements MarketFeedPort)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from trading.data.events import RawMarketCapture
from trading.domain.clock import Clock

__all__ = ["MockFyersMarketFeed"]


class MockFyersMarketFeed:
    """Deterministic Fyers-shaped captures without network calls."""

    def __init__(
        self,
        clock: Clock,
        *,
        nifty_ltp: str = "24500.00",
        vix_ltp: str = "12.50",
    ) -> None:
        self._clock = clock
        self._nifty_ltp = nifty_ltp
        self._vix_ltp = vix_ltp
        self.rest_call_count = 0

    def _capture(self, endpoint: str, payload: dict[str, Any]) -> RawMarketCapture:
        self.rest_call_count += 1
        return RawMarketCapture(
            capture_id=f"mock-{endpoint}",
            provider="mock-fyers",
            endpoint=endpoint,
            received_at=self._clock.now_utc(),
            payload=payload,
            http_status=200,
        )

    def fetch_option_chain(self, symbol: str) -> RawMarketCapture:
        return self._capture(
            "options-chain-v3",
            {
                "data": {
                    "optionsChain": [
                        {
                            "option_type": "CE",
                            "strike_price": 24500,
                            "ltp": 120,
                            "bid": 119,
                            "ask": 121,
                            "oi": 5000,
                            "volume": 200,
                            "iv": 0.14,
                            "delta": 0.52,
                            "gamma": 0.02,
                            "vega": 0.08,
                            "theta": -0.04,
                            "symbol": f"{symbol}24500CE",
                        },
                        {
                            "option_type": "PE",
                            "strike_price": 24500,
                            "ltp": 110,
                            "bid": 109,
                            "ask": 111,
                            "oi": 4800,
                            "volume": 180,
                            "iv": 0.13,
                            "delta": -0.48,
                            "gamma": 0.02,
                            "vega": 0.08,
                            "theta": -0.04,
                            "symbol": f"{symbol}24500PE",
                        },
                    ]
                }
            },
        )

    def fetch_depth(self, symbol: str) -> RawMarketCapture:
        bid = str(float(self._nifty_ltp) - 0.05)
        ask = str(float(self._nifty_ltp) + 0.05)
        return self._capture(
            "depth",
            {
                "data": {
                    "symbol": symbol,
                    "bid_price": [bid, bid, bid, bid, bid],
                    "bid_size": [100, 90, 80, 70, 60],
                    "ask_price": [ask, ask, ask, ask, ask],
                    "ask_size": [95, 85, 75, 65, 55],
                }
            },
        )

    def fetch_quotes(self, symbols: Sequence[str]) -> RawMarketCapture:
        rows = []
        for sym in symbols:
            ltp = self._vix_ltp if "VIX" in sym else self._nifty_ltp
            rows.append({"n": sym, "v": {"lp": ltp, "bid": ltp, "ask": ltp}})
        return self._capture("quotes", {"data": rows})

    def fetch_history(
        self,
        symbol: str,
        *,
        resolution: str,
        range_from: str,
        range_to: str,
    ) -> RawMarketCapture:
        _ = (symbol, resolution, range_from, range_to)
        return self._capture("history", {"data": {"candles": []}})

    def fetch_market_status(self) -> RawMarketCapture:
        return self._capture("market-status", {"data": {"market": "open"}})

    def fetch_expiry_dates(self, symbol: str) -> RawMarketCapture:
        return self._capture("expiry", {"data": {"expiryData": []}, "symbol": symbol})
