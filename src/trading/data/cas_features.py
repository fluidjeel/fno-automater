"""Close-auction microstructure features. Layer 1 only; no trade selection.

Keys match ``cas-microstructure-v1``. A missing input omits the key rather
than defaulting, so Layer 3 fails closed on ``DATA_GAP``.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from trading.data.events import CanonicalMarketEvent
from trading.data.prices import depth_best_ask, depth_best_bid, positive_decimal
from trading.domain.contracts import FeatureSnapshot

__all__ = [
    "CAS_FEATURE_KEYS",
    "CAS_FEATURE_SET_VERSION",
    "FEATURE_AUCTION_IMBALANCE",
    "FEATURE_MICROPRICE_EDGE_BPS",
    "FEATURE_QUOTE_INSTABILITY",
    "FEATURE_TRADE_FLOW_IMBALANCE",
    "cas_snapshot_version",
    "compute_cas_features",
    "select_prior_depth",
    "with_cas_feature_set",
]

CAS_FEATURE_SET_VERSION = "cas-microstructure-v1"
FEATURE_AUCTION_IMBALANCE = "cas_auction_imbalance"
FEATURE_TRADE_FLOW_IMBALANCE = "cas_trade_flow_imbalance"
FEATURE_MICROPRICE_EDGE_BPS = "cas_microprice_edge_bps"
FEATURE_QUOTE_INSTABILITY = "cas_quote_instability"
CAS_FEATURE_KEYS = (
    FEATURE_AUCTION_IMBALANCE,
    FEATURE_TRADE_FLOW_IMBALANCE,
    FEATURE_MICROPRICE_EDGE_BPS,
    FEATURE_QUOTE_INSTABILITY,
)

_SCALE = Decimal("0.0001")


def compute_cas_features(events: Sequence[CanonicalMarketEvent]) -> dict[str, Decimal]:
    """Compute declared CAS keys from timestamped depth and tick events."""
    depths = [event for event in events if event.event_type == "DEPTH_SNAPSHOT"]
    ticks = [event for event in events if event.event_type == "TICK"]
    if not depths:
        return {}
    current = depths[-1]
    prior = depths[-2] if len(depths) >= 2 else None
    features: dict[str, Decimal] = {}
    imbalance = _auction_imbalance(current)
    if imbalance is not None:
        features[FEATURE_AUCTION_IMBALANCE] = imbalance
    flow = _trade_flow_imbalance(current, prior, ticks)
    if flow is not None:
        features[FEATURE_TRADE_FLOW_IMBALANCE] = flow
    edge = _microprice_edge_bps(current)
    if edge is not None:
        features[FEATURE_MICROPRICE_EDGE_BPS] = edge
    instability = _quote_instability(current, prior)
    if instability is not None:
        features[FEATURE_QUOTE_INSTABILITY] = instability
    return features


def select_prior_depth(
    stored: Sequence[CanonicalMarketEvent],
    *,
    current: CanonicalMarketEvent,
) -> CanonicalMarketEvent | None:
    """Latest earlier DEPTH_SNAPSHOT. Absence stays absent; nothing is invented."""
    priors = [
        event
        for event in stored
        if event.event_type == "DEPTH_SNAPSHOT"
        and event.event_time < current.event_time
    ]
    if not priors:
        return None
    return max(priors, key=lambda event: event.event_time)


def cas_snapshot_version(features: dict[str, Decimal]) -> str | None:
    """Return the CAS feature-set version only when every declared key is present."""
    if all(key in features for key in CAS_FEATURE_KEYS):
        return CAS_FEATURE_SET_VERSION
    return None


def with_cas_feature_set(snapshot: FeatureSnapshot) -> FeatureSnapshot | None:
    """Stamp ``cas-microstructure-v1`` when the declared keys are complete."""
    if cas_snapshot_version(snapshot.features) is None:
        return None
    return snapshot.model_copy(update={"feature_set_version": CAS_FEATURE_SET_VERSION})


def _auction_imbalance(depth: CanonicalMarketEvent) -> Decimal | None:
    buy = _qty(depth.payload.get("total_buy_qty"))
    sell = _qty(depth.payload.get("total_sell_qty"))
    if buy is None or sell is None:
        return None
    total = buy + sell
    if total <= 0:
        return None
    return ((buy - sell) / total).quantize(_SCALE)


def _trade_flow_imbalance(
    current: CanonicalMarketEvent,
    prior: CanonicalMarketEvent | None,
    ticks: Sequence[CanonicalMarketEvent],
) -> Decimal | None:
    tick_flow = _tick_flow(ticks)
    if tick_flow is not None:
        return tick_flow
    if prior is None:
        return None
    delta_buy = _qty_delta(prior, current, "total_buy_qty")
    delta_sell = _qty_delta(prior, current, "total_sell_qty")
    if delta_buy is None or delta_sell is None:
        return None
    denom = abs(delta_buy) + abs(delta_sell)
    if denom <= 0:
        return Decimal("0")
    return ((delta_buy - delta_sell) / denom).quantize(_SCALE)


def _tick_flow(ticks: Sequence[CanonicalMarketEvent]) -> Decimal | None:
    if len(ticks) < 2:
        return None
    up = 0
    down = 0
    previous: Decimal | None = None
    for tick in ticks:
        last = positive_decimal(tick.payload.get("ltp"))
        if last is None:
            continue
        if previous is not None:
            if last > previous:
                up += 1
            elif last < previous:
                down += 1
        previous = last
    total = up + down
    if total <= 0:
        return None
    return (Decimal(up - down) / Decimal(total)).quantize(_SCALE)


def _microprice_edge_bps(depth: CanonicalMarketEvent) -> Decimal | None:
    bid = depth_best_bid(depth)
    ask = depth_best_ask(depth)
    if bid is None or ask is None or bid <= 0 or ask <= 0:
        return None
    mid = (bid + ask) / 2
    if mid <= 0:
        return None
    bid_size = _level_size(depth.payload.get("bid_levels"))
    ask_size = _level_size(depth.payload.get("ask_levels"))
    if bid_size is None or ask_size is None or bid_size + ask_size <= 0:
        return None
    micro = ((ask * bid_size) + (bid * ask_size)) / (bid_size + ask_size)
    return ((micro - mid) / mid * Decimal("10000")).quantize(_SCALE)


def _quote_instability(
    current: CanonicalMarketEvent,
    prior: CanonicalMarketEvent | None,
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
    return move.quantize(_SCALE)


def _mid(depth: CanonicalMarketEvent) -> Decimal | None:
    bid = depth_best_bid(depth)
    ask = depth_best_ask(depth)
    if bid is None or ask is None:
        return None
    return (bid + ask) / 2


def _qty(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except ArithmeticError:
        return None
    if parsed < 0:
        return None
    return parsed


def _qty_delta(
    prior: CanonicalMarketEvent,
    current: CanonicalMarketEvent,
    key: str,
) -> Decimal | None:
    before = _qty(prior.payload.get(key))
    after = _qty(current.payload.get(key))
    if before is None or after is None:
        return None
    return after - before


def _level_size(levels: Any) -> Decimal | None:
    if not isinstance(levels, list) or not levels:
        return None
    first = levels[0]
    if not isinstance(first, dict):
        return None
    return _qty(first.get("volume"))
