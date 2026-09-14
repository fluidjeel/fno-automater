"""Authoritative price resolution shared by the quality gate and snapshot builder."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from trading.data.events import CanonicalMarketEvent

__all__ = [
    "bar_close",
    "chain_spot",
    "depth_best_ask",
    "depth_best_bid",
    "positive_decimal",
    "quote_last",
    "resolve_last_price",
]


def positive_decimal(value: Any) -> Decimal | None:
    """Parse a strictly positive Decimal, rejecting bools and non-numerics."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal, str)):
        try:
            parsed = Decimal(str(value))
        except ArithmeticError:
            return None
        return parsed if parsed > 0 else None
    return None


def chain_spot(chain: CanonicalMarketEvent) -> Decimal | None:
    """Underlying price carried by the option-chain row without an option type."""
    strikes = chain.payload.get("strikes", [])
    if not isinstance(strikes, list):
        return None
    for row in strikes:
        if isinstance(row, dict) and row.get("option_type") in {"", None}:
            return positive_decimal(row.get("ltp", row.get("fp")))
    if strikes and isinstance(strikes[0], dict):
        return positive_decimal(strikes[0].get("ltp", strikes[0].get("fp")))
    return None


def quote_last(quote: CanonicalMarketEvent) -> Decimal | None:
    """Last traded price from the first quote row."""
    quotes = quote.payload.get("quotes", [])
    if isinstance(quotes, list) and quotes and isinstance(quotes[0], dict):
        return positive_decimal(quotes[0].get("lp", quotes[0].get("ltp")))
    return None


def bar_close(bar: CanonicalMarketEvent) -> Decimal | None:
    """Close of the most recent bar."""
    bars = bar.payload.get("bars", [])
    if isinstance(bars, list) and bars and isinstance(bars[-1], dict):
        return positive_decimal(bars[-1].get("close"))
    return None


def depth_best_bid(depth: CanonicalMarketEvent) -> Decimal | None:
    """Best bid price from the depth ladder."""
    bids = depth.payload.get("bid_levels", [])
    if isinstance(bids, list) and bids and isinstance(bids[0], dict):
        return positive_decimal(bids[0].get("price"))
    return None


def depth_best_ask(depth: CanonicalMarketEvent) -> Decimal | None:
    """Best ask price from the depth ladder."""
    asks = depth.payload.get("ask_levels", [])
    if isinstance(asks, list) and asks and isinstance(asks[0], dict):
        return positive_decimal(asks[0].get("price"))
    return None


def resolve_last_price(
    *,
    chain: CanonicalMarketEvent,
    quote: CanonicalMarketEvent | None,
    bar: CanonicalMarketEvent | None,
) -> Decimal | None:
    """Return the authoritative last price, or None when no source supplies one.

    Precedence matches the snapshot builder: a completed bar close outranks a
    quote, which outranks the chain's underlying row. None means the cycle has
    no tradable reference price and must not be priced against.
    """
    if bar is not None:
        close = bar_close(bar)
        if close is not None:
            return close
    if quote is not None:
        last = quote_last(quote)
        if last is not None:
            return last
    return chain_spot(chain)
