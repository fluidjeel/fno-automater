"""Fetch and normalize order book depth."""

from __future__ import annotations

from datetime import UTC
from decimal import Decimal
from typing import Any

from trading.data.events import RawMarketCapture
from trading.data.minute_backbone.models import DepthSnapshot, snapshot_id

__all__ = ["build_depth_snapshot"]


def _level(book: dict[str, Any], side: str, index: int) -> tuple[str, int]:
    key_price = f"{side}_price"
    key_size = f"{side}_size"
    prices = book.get(key_price) or book.get(f"{side}Price") or []
    sizes = book.get(key_size) or book.get(f"{side}Size") or []
    price = "0"
    size = 0
    if isinstance(prices, list) and len(prices) > index:
        price = str(Decimal(str(prices[index])))
    if isinstance(sizes, list) and len(sizes) > index:
        size = int(sizes[index])
    return price, size


def build_depth_snapshot(capture: RawMarketCapture, *, symbol: str) -> DepthSnapshot:
    """Map Fyers depth REST capture to DepthSnapshot."""
    data = capture.payload.get("data", capture.payload)
    book = data if isinstance(data, dict) else {}
    bid1, bs1 = _level(book, "bid", 0)
    bid2, bs2 = _level(book, "bid", 1)
    bid3, bs3 = _level(book, "bid", 2)
    bid4, bs4 = _level(book, "bid", 3)
    bid5, bs5 = _level(book, "bid", 4)
    ask1, as1 = _level(book, "ask", 0)
    ask2, as2 = _level(book, "ask", 1)
    ask3, as3 = _level(book, "ask", 2)
    ask4, as4 = _level(book, "ask", 3)
    ask5, as5 = _level(book, "ask", 4)
    mid = (Decimal(bid1) + Decimal(ask1)) / Decimal("2") if bid1 != "0" and ask1 != "0" else Decimal("0")
    spread_bps = "0"
    if mid > 0:
        spread_bps = str(((Decimal(ask1) - Decimal(bid1)) / mid) * Decimal("10000"))
    body = {"symbol": symbol, "bid1": bid1, "ask1": ask1}
    return DepthSnapshot(
        snapshot_id=snapshot_id(body),
        symbol=symbol,
        as_of_utc=capture.received_at.astimezone(UTC),
        bid_price_1=bid1,
        bid_size_1=bs1,
        bid_price_2=bid2,
        bid_size_2=bs2,
        bid_price_3=bid3,
        bid_size_3=bs3,
        bid_price_4=bid4,
        bid_size_4=bs4,
        bid_price_5=bid5,
        bid_size_5=bs5,
        ask_price_1=ask1,
        ask_size_1=as1,
        ask_price_2=ask2,
        ask_size_2=as2,
        ask_price_3=ask3,
        ask_size_3=as3,
        ask_price_4=ask4,
        ask_size_4=as4,
        ask_price_5=ask5,
        ask_size_5=as5,
        spread_bps=spread_bps,
    )
