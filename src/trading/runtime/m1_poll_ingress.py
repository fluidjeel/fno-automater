"""DISC-B1: feed M1 provider events from the session poll when flat."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from trading.domain.contracts import FeatureSnapshot, MarketState
from trading.domain.enums import InstrumentKind, ModeId, OptionType
from trading.runtime.cas_event_path import CasEventDrivenConfig, active_m1_window
from trading.runtime.four_mode_producers import _candidates_for_mode

if TYPE_CHECKING:
    from trading.runtime.paper_runner import PaperCycleResult
    from trading.runtime.paper_session import PaperSession

__all__ = [
    "feed_m1_poll_provider_event",
    "m1_poll_watch_symbols",
]


def m1_poll_watch_symbols(
    snapshots: Mapping[str, FeatureSnapshot],
    market_state: MarketState,
    *,
    config: CasEventDrivenConfig,
) -> tuple[str, ...]:
    """Symbols to subscribe for M1 ingress when no open M1 position exists."""
    selected = _select_m1_poll_snapshot(snapshots, market_state, config=config)
    if selected is None:
        return ()
    return (selected.contract.symbol,)


def feed_m1_poll_provider_event(
    session: PaperSession,
    snapshots: Mapping[str, FeatureSnapshot],
    market_state: MarketState,
    *,
    now: datetime,
) -> PaperCycleResult | None:
    """Submit one M1 event from the latest poll snapshots (honest quote times)."""
    cfg = getattr(getattr(session, "session_config", None), "cas_event_driven", None)
    if cfg is None or not cfg.enabled or active_m1_window(now, cfg) is None:
        return None
    selected = _select_m1_poll_snapshot(snapshots, market_state, config=cfg)
    if selected is None:
        return None
    quote = selected.market
    quote_time = getattr(quote, "exchange_ts", None) or getattr(quote, "as_of", None)
    if not isinstance(quote_time, datetime):
        return None
    bid = quote.bid
    ask = quote.ask
    if bid is None or ask is None or bid.value <= 0 or ask.value <= 0:
        return None
    return session.on_provider_quote(
        selected.contract.symbol,
        quote,
        receive_time=now,
    )


def _select_m1_poll_snapshot(
    snapshots: Mapping[str, FeatureSnapshot],
    market_state: MarketState,
    *,
    config: CasEventDrivenConfig,
) -> FeatureSnapshot | None:
    option_type = _direction_type(market_state)
    if option_type is None:
        return None
    near = _candidates_for_mode(tuple(snapshots.values()), mode_id=ModeId.M1_CAS)
    eligible: list[tuple[FeatureSnapshot, Decimal]] = []
    for item in near:
        if item.contract.instrument_kind is not InstrumentKind.OPTION:
            continue
        if item.contract.option_type is not option_type:
            continue
        delta = _abs_delta(item)
        if delta is None:
            continue
        if delta < config.prefer_delta_min or delta > config.prefer_delta_max:
            continue
        ask = item.market.ask
        if ask is None or ask.value <= 0:
            continue
        eligible.append((item, delta / ask.value))
    if not eligible:
        return None
    eligible.sort(key=lambda row: (-row[1], row[0].contract.symbol))
    return eligible[0][0]


def _direction_type(market: MarketState) -> OptionType | None:
    if market.trend.value == "UP":
        return OptionType.CALL
    if market.trend.value == "DOWN":
        return OptionType.PUT
    return None


def _abs_delta(snapshot: FeatureSnapshot) -> Decimal | None:
    derivatives = snapshot.derivatives
    if derivatives is None or derivatives.greeks is None:
        return None
    delta = derivatives.greeks.delta
    if delta is None:
        return None
    return abs(delta)
