"""DEPTH_ONLY CAS microstructure features. No aggressor inference."""

from __future__ import annotations

from decimal import Decimal

from trading.data.cas_depth.contracts import (
    CAS_DEPTH_FEATURE_KEYS,
    FEATURE_DEPTH_SLOPE,
    FEATURE_LIQUIDITY_GAP,
    FEATURE_MICROPRICE,
    FEATURE_QUOTE_STABILITY,
    FEATURE_REPLENISHMENT,
    FEATURE_SPREAD,
    FEATURE_TOP5_IMBALANCE,
    FEATURE_TOP50_IMBALANCE,
    DepthLevel,
    NormalizedDepthUpdate,
)

__all__ = [
    "FEATURE_DEPTH_SLOPE",
    "FEATURE_LIQUIDITY_GAP",
    "FEATURE_MICROPRICE",
    "FEATURE_QUOTE_STABILITY",
    "FEATURE_REPLENISHMENT",
    "FEATURE_SPREAD",
    "FEATURE_TOP5_IMBALANCE",
    "FEATURE_TOP50_IMBALANCE",
    "compute_depth_only_features",
    "depth_features_complete",
]

_SCALE = Decimal("0.0001")
_BPS = Decimal("10000")


def compute_depth_only_features(
    current: NormalizedDepthUpdate,
    prior: NormalizedDepthUpdate | None,
) -> dict[str, Decimal]:
    """Compute depth-only features; omit keys when inputs are insufficient."""
    features: dict[str, Decimal] = {}
    top5 = _imbalance(current.bid_levels, current.ask_levels, depth=5)
    if top5 is not None:
        features[FEATURE_TOP5_IMBALANCE] = top5
    top50 = _imbalance(current.bid_levels, current.ask_levels, depth=50)
    if top50 is not None:
        features[FEATURE_TOP50_IMBALANCE] = top50
    micro = _microprice(current.bid_levels, current.ask_levels)
    if micro is not None:
        features[FEATURE_MICROPRICE] = micro
    spread = _spread(current.bid_levels, current.ask_levels)
    if spread is not None:
        features[FEATURE_SPREAD] = spread
    slope = _depth_slope(current.bid_levels, current.ask_levels)
    if slope is not None:
        features[FEATURE_DEPTH_SLOPE] = slope
    gap = _liquidity_gap(current.bid_levels, current.ask_levels)
    if gap is not None:
        features[FEATURE_LIQUIDITY_GAP] = gap
    stability = _quote_stability(current, prior)
    if stability is not None:
        features[FEATURE_QUOTE_STABILITY] = stability
    replenishment = _replenishment(current, prior)
    if replenishment is not None:
        features[FEATURE_REPLENISHMENT] = replenishment
    return features


def depth_features_complete(features: dict[str, Decimal]) -> bool:
    """True when every declared depth-only key is present."""
    return all(key in features for key in CAS_DEPTH_FEATURE_KEYS)


def _imbalance(
    bids: tuple[DepthLevel, ...],
    asks: tuple[DepthLevel, ...],
    *,
    depth: int,
) -> Decimal | None:
    bid_qty = sum(level.quantity for level in bids[:depth])
    ask_qty = sum(level.quantity for level in asks[:depth])
    total = bid_qty + ask_qty
    if total <= 0:
        return None
    return (Decimal(bid_qty - ask_qty) / Decimal(total)).quantize(_SCALE)


def _microprice(
    bids: tuple[DepthLevel, ...],
    asks: tuple[DepthLevel, ...],
) -> Decimal | None:
    if not bids or not asks:
        return None
    best_bid = bids[0]
    best_ask = asks[0]
    denom = best_bid.quantity + best_ask.quantity
    if denom <= 0:
        return None
    micro = (
        (best_ask.price * Decimal(best_bid.quantity))
        + (best_bid.price * Decimal(best_ask.quantity))
    ) / Decimal(denom)
    return micro.quantize(_SCALE)


def _spread(
    bids: tuple[DepthLevel, ...],
    asks: tuple[DepthLevel, ...],
) -> Decimal | None:
    if not bids or not asks:
        return None
    mid = (bids[0].price + asks[0].price) / 2
    if mid <= 0:
        return None
    return ((asks[0].price - bids[0].price) / mid * _BPS).quantize(_SCALE)


def _depth_slope(
    bids: tuple[DepthLevel, ...],
    asks: tuple[DepthLevel, ...],
) -> Decimal | None:
    bid_slope = _side_slope(bids)
    ask_slope = _side_slope(asks)
    if bid_slope is None or ask_slope is None:
        return None
    return (bid_slope - ask_slope).quantize(_SCALE)


def _side_slope(levels: tuple[DepthLevel, ...]) -> Decimal | None:
    if len(levels) < 2:
        return None
    first = levels[0].quantity
    last = levels[-1].quantity
    if first <= 0:
        return None
    return (Decimal(last) / Decimal(first)).quantize(_SCALE)


def _liquidity_gap(
    bids: tuple[DepthLevel, ...],
    asks: tuple[DepthLevel, ...],
) -> Decimal | None:
    if not bids or not asks:
        return None
    return (asks[0].price - bids[0].price).quantize(_SCALE)


def _quote_stability(
    current: NormalizedDepthUpdate,
    prior: NormalizedDepthUpdate | None,
) -> Decimal | None:
    if prior is None:
        return None
    current_mid = _mid(current)
    prior_mid = _mid(prior)
    if current_mid is None or prior_mid is None or prior_mid <= 0:
        return None
    move = abs(current_mid - prior_mid) / prior_mid
    if move > 1:
        move = Decimal("1")
    return (Decimal("1") - move).quantize(_SCALE)


def _replenishment(
    current: NormalizedDepthUpdate,
    prior: NormalizedDepthUpdate | None,
) -> Decimal | None:
    if prior is None:
        return None
    if not current.bid_levels or not prior.bid_levels:
        return None
    if not current.ask_levels or not prior.ask_levels:
        return None
    bid_refill = Decimal(current.bid_levels[0].quantity) / Decimal(
        max(prior.bid_levels[0].quantity, 1)
    )
    ask_refill = Decimal(current.ask_levels[0].quantity) / Decimal(
        max(prior.ask_levels[0].quantity, 1)
    )
    return ((bid_refill + ask_refill) / 2).quantize(_SCALE)


def _mid(update: NormalizedDepthUpdate) -> Decimal | None:
    if not update.bid_levels or not update.ask_levels:
        return None
    return (update.bid_levels[0].price + update.ask_levels[0].price) / 2
