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
) -> tuple[tuple[DepthLevel, ...], tuple[DepthLevel, ...], bool]:
    """Dedupe by price, then sort bids descending and asks ascending."""
    deduped_bids, bid_dupes = _dedupe_levels_by_price(bids)
    deduped_asks, ask_dupes = _dedupe_levels_by_price(asks)
    bid_sorted = tuple(
        sorted(deduped_bids, key=lambda level: level.price, reverse=True)
    )
    ask_sorted = tuple(sorted(deduped_asks, key=lambda level: level.price))
    return bid_sorted, ask_sorted, bid_dupes or ask_dupes


def normalize_tbt_depth(
    symbol: str,
    depth: Any,
    *,
    receive_time: datetime,
    feed: str = "tbt_ws",
) -> tuple[NormalizedDepthUpdate | None, bool]:
    """Map SDK ``Depth`` object to a normalized update."""
    # FyersTbtSocket merges incremental diffs into fixed 50-slot arrays
    # (tbt_ws.py DataStore.updateDepth + Depth._addDepth): only indices present
    # in the wire packet are overwritten; stale slots are never cleared. When
    # qty goes to zero the SDK updates askqty/bidqty but does not clear price,
    # so price-only slots must be dropped. Partial diffs also leave higher-index
    # better prices while lower indices keep stale worse BBO levels, violating
    # exchange rank order (index 0 = best).
    is_snapshot = bool(depth.snapshot)
    indexed_bids = _extract_tbt_side(
        depth.bidprice,
        depth.bidqty,
        depth.bidordn,
        is_bid=True,
        is_snapshot=is_snapshot,
    )
    indexed_asks = _extract_tbt_side(
        depth.askprice,
        depth.askqty,
        depth.askordn,
        is_bid=False,
        is_snapshot=is_snapshot,
    )
    had_duplicate_prices = _has_duplicate_prices(indexed_bids) or _has_duplicate_prices(
        indexed_asks
    )
    pruned_bids = _drop_stale_rank_violations(indexed_bids, is_bid=True)
    pruned_asks = _drop_stale_rank_violations(indexed_asks, is_bid=False)
    bids, bid_dupes = _levels_from_indexed(pruned_bids)
    asks, ask_dupes = _levels_from_indexed(pruned_asks)
    bid_levels = tuple(sorted(bids, key=lambda level: level.price, reverse=True))
    ask_levels = tuple(sorted(asks, key=lambda level: level.price))
    if not bid_levels and not ask_levels:
        return None, False
    exchange_ts = _epoch_to_utc(depth.timestamp)
    return (
        NormalizedDepthUpdate(
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
            is_snapshot=is_snapshot,
            feed=feed,
        ),
        had_duplicate_prices or bid_dupes or ask_dupes,
    )


def normalize_data_ws_depth(
    message: dict[str, Any],
    *,
    receive_time: datetime,
    feed: str = "data_ws",
) -> tuple[NormalizedDepthUpdate | None, bool]:
    """Map data-socket ``DepthUpdate`` dict to a normalized update."""
    symbol = message.get("symbol")
    if not isinstance(symbol, str):
        return None, False
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
    bid_levels, ask_levels, had_duplicate_prices = sort_levels(bids, asks)
    if not bid_levels and not ask_levels:
        return None, False
    exchange_ts = receive_time
    exch_feed = message.get("exch_feed_time")
    if isinstance(exch_feed, (int, float)):
        exchange_ts = _epoch_to_utc(int(exch_feed))
    ltp = _optional_decimal(message.get("ltp"))
    return (
        NormalizedDepthUpdate(
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
        ),
        had_duplicate_prices,
    )


def _extract_tbt_side(
    prices: list[float],
    qtys: list[int | float],
    orders: list[int | float],
    *,
    is_bid: bool,
    is_snapshot: bool,
) -> list[tuple[int, DepthLevel]]:
    """Collect qty>0 TBT slots; on snapshots truncate trailing empty tail."""
    _ = is_bid
    indexed: list[tuple[int, DepthLevel]] = []
    for index in range(50):
        qty = int(qtys[index])
        price_raw = prices[index]
        if price_raw <= 0 and qty <= 0:
            if is_snapshot:
                break
            continue
        if qty <= 0 or price_raw <= 0:
            continue
        price = Decimal(str(price_raw)).quantize(_SCALE)
        if price <= 0:
            continue
        indexed.append(
            (
                index,
                DepthLevel(
                    price=price,
                    quantity=qty,
                    orders=int(orders[index]) if orders[index] else None,
                ),
            )
        )
    return indexed


def _drop_stale_rank_violations(
    indexed: list[tuple[int, DepthLevel]],
    *,
    is_bid: bool,
) -> list[tuple[int, DepthLevel]]:
    """Drop lower-index slots invalidated by a better price at a higher index."""
    stale_indices: set[int] = set()
    for slot_index, level in indexed:
        for other_index, other in indexed:
            if other_index <= slot_index:
                continue
            if is_bid:
                if other.price > level.price:
                    stale_indices.add(slot_index)
                    break
            elif other.price < level.price:
                stale_indices.add(slot_index)
                break
    return [
        (slot_index, level)
        for slot_index, level in indexed
        if slot_index not in stale_indices
    ]


def _levels_from_indexed(
    indexed: list[tuple[int, DepthLevel]],
) -> tuple[list[DepthLevel], bool]:
    seen: set[Decimal] = set()
    levels: list[DepthLevel] = []
    had_duplicates = False
    for _, level in sorted(indexed, key=lambda item: item[0]):
        if level.price in seen:
            had_duplicates = True
            continue
        seen.add(level.price)
        levels.append(level)
    return levels, had_duplicates


def _has_duplicate_prices(indexed: list[tuple[int, DepthLevel]]) -> bool:
    seen: set[Decimal] = set()
    for _, level in indexed:
        if level.price in seen:
            return True
        seen.add(level.price)
    return False


def _dedupe_levels_by_price(
    levels: list[DepthLevel],
) -> tuple[list[DepthLevel], bool]:
    """Keep the lowest slot index when the same price appears more than once."""
    seen: set[Decimal] = set()
    deduped: list[DepthLevel] = []
    had_duplicates = False
    for level in levels:
        if level.price in seen:
            had_duplicates = True
            continue
        seen.add(level.price)
        deduped.append(level)
    return deduped, had_duplicates


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
