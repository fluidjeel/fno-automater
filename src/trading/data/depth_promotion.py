"""Score strikes and apply promotion/demotion hysteresis for WS depth."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from trading.config.depth_promotion import DepthPromotionConfig
from trading.data.events import CanonicalMarketEvent
from trading.data.prices import chain_spot, positive_decimal

__all__ = [
    "DepthPromotionHealth",
    "DepthPromotionState",
    "DepthSetPromoter",
    "StrikeScore",
    "score_chain_strikes",
]


@dataclass(frozen=True, slots=True)
class StrikeScore:
    """One scored option symbol from the chain snapshot."""

    symbol: str
    score: Decimal


@dataclass(frozen=True, slots=True)
class DepthPromotionHealth:
    """Promotion telemetry for session health output."""

    promoted_symbols: tuple[str, ...]
    top_symbols: tuple[str, ...]
    depth_set_size: int

    def as_dict(self) -> dict[str, object]:
        return {
            "promoted_symbols": list(self.promoted_symbols),
            "top_symbols": list(self.top_symbols),
            "depth_set_size": self.depth_set_size,
        }


@dataclass
class DepthPromotionState:
    """Mutable hysteresis state across poll cycles."""

    promoted: frozenset[str] = frozenset()
    in_top_streak: dict[str, int] = field(default_factory=dict)
    out_streak: dict[str, int] = field(default_factory=dict)
    last_top: frozenset[str] = frozenset()
    last_added: frozenset[str] = frozenset()
    last_removed: frozenset[str] = frozenset()


class DepthSetPromoter:
    """Maintain a promoted depth set with enter/exit hysteresis."""

    def __init__(self, config: DepthPromotionConfig) -> None:
        self._config = config
        self._state = DepthPromotionState()

    @property
    def state(self) -> DepthPromotionState:
        return self._state

    @property
    def promoted(self) -> frozenset[str]:
        return self._state.promoted

    def poll(self, chain: CanonicalMarketEvent) -> frozenset[str]:
        """Score the chain, update hysteresis, and return the promoted set."""
        spot = chain_spot(chain)
        if spot is None:
            return self._state.promoted
        ranked = score_chain_strikes(chain, spot, self._config)
        top = frozenset(item.symbol for item in ranked[: self._config.depth_set_size])
        previous = self._state.promoted
        promoted = set(previous)

        for symbol in top:
            self._state.in_top_streak[symbol] = (
                self._state.in_top_streak.get(symbol, 0) + 1
            )
            self._state.out_streak[symbol] = 0

        for symbol in previous - top:
            self._state.out_streak[symbol] = self._state.out_streak.get(symbol, 0) + 1
            self._state.in_top_streak[symbol] = 0

        for symbol in list(promoted):
            if symbol not in top and (
                self._state.out_streak.get(symbol, 0) >= self._config.demote_after_polls
            ):
                promoted.discard(symbol)

        for item in ranked[: self._config.depth_set_size]:
            symbol = item.symbol
            if symbol in promoted:
                continue
            if (
                self._state.in_top_streak.get(symbol, 0)
                < self._config.promote_after_polls
            ):
                continue
            if len(promoted) >= self._config.depth_set_size:
                break
            promoted.add(symbol)

        new_promoted = frozenset(promoted)
        self._state.last_added = new_promoted - previous
        self._state.last_removed = previous - new_promoted
        self._state.last_top = top
        self._state.promoted = new_promoted
        return new_promoted

    def health(self) -> DepthPromotionHealth:
        return DepthPromotionHealth(
            promoted_symbols=tuple(sorted(self._state.promoted)),
            top_symbols=tuple(sorted(self._state.last_top)),
            depth_set_size=self._config.depth_set_size,
        )


def score_chain_strikes(
    chain: CanonicalMarketEvent,
    spot: Decimal,
    config: DepthPromotionConfig,
) -> list[StrikeScore]:
    """Rank option legs by weighted OI, volume and ATM proximity."""
    rows = chain.payload.get("strikes", [])
    if not isinstance(rows, list):
        return []
    weights = config.score_weights
    scored: list[StrikeScore] = []
    max_oi = Decimal(0)
    max_volume = Decimal(0)
    parsed: list[tuple[str, Decimal, Decimal, Decimal]] = []
    for row in rows:
        if not isinstance(row, dict) or row.get("option_type") not in {"CE", "PE"}:
            continue
        symbol = row.get("symbol")
        if not isinstance(symbol, str) or not symbol:
            continue
        strike = positive_decimal(row.get("strike_price"))
        if strike is None:
            continue
        oi = positive_decimal(row.get("oi")) or Decimal(0)
        volume = positive_decimal(row.get("volume")) or Decimal(0)
        max_oi = max(max_oi, oi)
        max_volume = max(max_volume, volume)
        proximity = _proximity_score(strike, spot)
        parsed.append((symbol, oi, volume, proximity))
    for symbol, oi, volume, proximity in parsed:
        oi_norm = oi / max_oi if max_oi > 0 else Decimal(0)
        vol_norm = volume / max_volume if max_volume > 0 else Decimal(0)
        score = (
            weights.open_interest * oi_norm
            + weights.volume * vol_norm
            + weights.proximity * proximity
        )
        scored.append(StrikeScore(symbol=symbol, score=score))
    scored.sort(key=lambda item: (-item.score, item.symbol))
    return scored


def _proximity_score(strike: Decimal, spot: Decimal) -> Decimal:
    if spot <= 0:
        return Decimal(0)
    distance = abs(strike - spot) / spot
    if distance >= Decimal("1"):
        return Decimal(0)
    return Decimal(1) - distance
