"""Normalize FYERS TBT and data-socket depth into price-sorted books."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from trading.data.cas_depth.contracts import (
    DepthLevel,
    NormalizedDepthUpdate,
    TradeAggressor,
)

__all__ = [
    "normalize_data_ws_depth",
    "normalize_tbt_depth",
    "sort_levels",
]

_SCALE = Decimal("0.01")


def sort_levels(
    bids: list[DepthLevel],
    asks: list[DepthLevel],
) -> tuple[tuple[DepthLevel, ...], tuple[DepthLevel, ...]]:
    """Sort bids descending and asks ascending by price."""
    bid_sorted = tuple(sorted(bids, key=lambda level: level.price, reverse=True))
    ask_sorted = tuple(sorted(asks, key=lambda level: level.price))
    return bid_sorted, ask_sorted


def normalize_tbt_depth(
    symbol: str,
    depth: Any,
    *,
    receive_time: datetime,
    feed: str = "tbt_ws",
) -> NormalizedDepthUpdate | None:
    """Map SDK ``Depth`` object to a normalized update."""
    bids = [
        DepthLevel(
            price=Decimal(str(depth.bidprice[i])).quantize(_SCALE),
            quantity=int(depth.bidqty[i]),
            orders=int(depth.bidordn[i]) if depth.bidordn[i] else None,
        )
        for i in range(50)
        if depth.bidprice[i] or depth.bidqty[i]
    ]
    asks = [
        DepthLevel(
            price=Decimal(str(depth.askprice[i])).quantize(_SCALE),
            quantity=int(depth.askqty[i]),
            orders=int(depth.askordn[i]) if depth.askordn[i] else None,
        )
        for i in range(50)
        if depth.askprice[i] or depth.askqty[i]
    ]
    bid_levels, ask_levels = sort_levels(bids, asks)
    if not bid_levels and not ask_levels:
        return None
    exchange_ts = _epoch_to_utc(depth.timestamp)
    return NormalizedDepthUpdate(
        symbol=symbol,
        exchange_timestamp=exchange_ts,
        receive_timestamp=receive_time,
        sequence=int(depth.seqNo) if depth.seqNo else None,
        bid_levels=bid_levels,
        ask_levels=ask_levels,
        ltp=None,
        last_quantity=None,
        total_volume=None,
        open_interest=None,
        trade_aggressor=TradeAggressor.UNKNOWN,
        is_snapshot=bool(depth.snapshot),
        feed=feed,
    )


def normalize_data_ws_depth(
    message: dict[str, Any],
    *,
    receive_time: datetime,
    feed: str = "data_ws",
) -> NormalizedDepthUpdate | None:
    """Map data-socket ``DepthUpdate`` dict to a normalized update."""
    symbol = message.get("symbol")
    if not isinstance(symbol, str):
        return None
    bids: list[DepthLevel] = []
    asks: list[DepthLevel] = []
    for index in range(1, 6):
        bid_price = message.get(f"bid_price{index}")
        bid_size = message.get(f"bid_size{index}")
        if bid_price is not None and bid_size is not None:
            bids.append(
                DepthLevel(
                    price=Decimal(str(bid_price)).quantize(_SCALE),
                    quantity=int(bid_size),
                    orders=_optional_int(message.get(f"bid_order{index}")),
                )
            )
        ask_price = message.get(f"ask_price{index}")
        ask_size = message.get(f"ask_size{index}")
        if ask_price is not None and ask_size is not None:
            asks.append(
                DepthLevel(
                    price=Decimal(str(ask_price)).quantize(_SCALE),
                    quantity=int(ask_size),
                    orders=_optional_int(message.get(f"ask_order{index}")),
                )
            )
    bid_levels, ask_levels = sort_levels(bids, asks)
    if not bid_levels and not ask_levels:
        return None
    exchange_ts = receive_time
    exch_feed = message.get("exch_feed_time")
    if isinstance(exch_feed, (int, float)):
        exchange_ts = _epoch_to_utc(int(exch_feed))
    ltp = _optional_decimal(message.get("ltp"))
    return NormalizedDepthUpdate(
        symbol=symbol,
        exchange_timestamp=exchange_ts,
        receive_timestamp=receive_time,
        sequence=None,
        bid_levels=bid_levels,
        ask_levels=ask_levels,
        ltp=ltp,
        last_quantity=_optional_int(message.get("last_traded_qty")),
        total_volume=_optional_int(message.get("vol_traded_today")),
        open_interest=_optional_int(message.get("oi")),
        trade_aggressor=TradeAggressor.UNKNOWN,
        is_snapshot=message.get("type") == "dp",
        feed=feed,
    )


def _epoch_to_utc(value: int) -> datetime:
    if value > 1_000_000_000_000:
        return datetime.fromtimestamp(value / 1000.0, tz=UTC)
    return datetime.fromtimestamp(value, tz=UTC)


def _optional_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _optional_decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value)).quantize(_SCALE)
    except ArithmeticError:
        return None
