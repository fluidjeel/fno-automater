"""Classify tick direction from top-of-book."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from trading.data.tick_consumer.models import SignedTick, TickEvent, TickSign

__all__ = ["sign_tick"]


def sign_tick(
    tick: TickEvent,
    *,
    bid_price_1: str | None,
    ask_price_1: str | None,
    depth_as_of_utc: datetime | None,
    depth_max_age_ms: int,
    now: datetime,
) -> SignedTick:
    """Apply deterministic signing rules from the tick consumer spec."""
    if (
        depth_as_of_utc is None
        or bid_price_1 is None
        or ask_price_1 is None
        or int((now - depth_as_of_utc).total_seconds() * 1000) > depth_max_age_ms
    ):
        return SignedTick(
            **tick.model_dump(),
            sign=TickSign.UNKNOWN,
            sign_confidence="0",
        )
    bid = Decimal(bid_price_1)
    ask = Decimal(ask_price_1)
    ltp = Decimal(tick.ltp)
    mid = (bid + ask) / Decimal("2")
    if ltp >= ask:
        return SignedTick(**tick.model_dump(), sign=TickSign.BUY, sign_confidence="1")
    if ltp <= bid:
        return SignedTick(**tick.model_dump(), sign=TickSign.SELL, sign_confidence="1")
    if ltp > mid:
        return SignedTick(**tick.model_dump(), sign=TickSign.BUY, sign_confidence="0.5")
    if ltp < mid:
        return SignedTick(**tick.model_dump(), sign=TickSign.SELL, sign_confidence="0.5")
    return SignedTick(
        **tick.model_dump(),
        sign=TickSign.UNKNOWN,
        sign_confidence="0",
    )
