"""Single websocket multiplex for platter ticks."""

from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from trading.data.config import load_data_pipeline_config
from trading.data.features.delta_state import load_delta_state, save_delta_state
from trading.data.fyers.ws import FyersTickStream
from trading.data.platter_pipeline.depth_hint import read_depth_hint
from trading.data.platter_pipeline.session import ist_session_date
from trading.data.settings import FyersSettings
from trading.data.storage.parquet_store import JsonlEventStore
from trading.data.tick_consumer.bars_1m import BarAggregator
from trading.data.tick_consumer.delta import DeltaTracker
from trading.data.tick_consumer.models import TickEvent, TickHealthEvent
from trading.data.tick_consumer.signing import sign_tick
from trading.data.tick_consumer.store import TickStore
from trading.domain.clock import WallClock

__all__ = ["run_tick_multiplex_daemon"]


def _tick_from_canonical(message: dict[str, Any], *, symbol: str, received_at: datetime) -> TickEvent:
    ltp = message.get("ltp") or message.get("last_price") or "0"
    vol = int(message.get("vol_traded_today") or message.get("volume") or 0)
    trade_size = int(message.get("last_traded_qty") or message.get("trade_size") or 0)
    return TickEvent(
        symbol=symbol,
        ltp=str(ltp),
        volume=vol,
        trade_size=trade_size,
        exchange_ts=received_at,
        received_at=received_at,
    )


def _apply_depth_hint(repo_root: Path, depth_cache: dict[str, Any]) -> None:
    hint = read_depth_hint(repo_root)
    if hint is None:
        return
    depth_cache["bid"] = hint.get("bid_price_1")
    depth_cache["ask"] = hint.get("ask_price_1")
    raw = hint.get("as_of_utc")
    if raw is not None:
        try:
            depth_cache["as_of"] = datetime.fromisoformat(str(raw))
        except ValueError:
            depth_cache["as_of"] = None


def run_tick_multiplex_daemon(
    repo_root: Path,
    *,
    symbol: str,
    stop: threading.Event | None = None,
) -> None:
    """Run websocket tick path with signing, bars, delta, and parquet store."""
    pipeline = load_data_pipeline_config(repo_root / "config" / "data_pipeline.yaml")
    settings = FyersSettings.from_repo_root_with_cache(repo_root)
    clock = WallClock()
    store_events = JsonlEventStore(repo_root / pipeline.storage.root)
    stream = FyersTickStream(
        settings,
        clock,
        store_events,
        repo_root=repo_root,
        normalization_version=pipeline.normalization_version,
        channel=pipeline.fyers.ws_channel,
        reconnect=pipeline.fyers.ws_reconnect,
        reconnect_attempts=pipeline.fyers.ws_reconnect_attempts,
        reconnect_backoff_seconds=pipeline.fyers.ws_reconnect_backoff_seconds,
    )
    tick_store = TickStore(repo_root)
    bars = BarAggregator(symbol)
    halt = stop or threading.Event()
    depth_cache: dict[str, Any] = {
        "bid": None,
        "ask": None,
        "as_of": None,
    }
    session_date = ist_session_date(clock.now_utc())
    persisted = load_delta_state(repo_root, session_date.isoformat())
    delta = DeltaTracker(session_date)
    if persisted is not None and persisted.session_date == session_date:
        delta.state.session_buy_volume = persisted.session_buy_volume
        delta.state.session_sell_volume = persisted.session_sell_volume
        delta.state.session_delta = persisted.session_delta
        delta.state.cumulative_delta = persisted.cumulative_delta
        delta.state.delta_5m = persisted.delta_5m

    while not halt.is_set():
        now = clock.now_utc()
        session_date = ist_session_date(now)
        _apply_depth_hint(repo_root, depth_cache)
        result = stream.collect(
            symbol,
            max_ticks=10_000_000,
            duration_seconds=3600,
            tick_max_age_ms=60_000,
            stop=halt,
        )
        if not result.ticks:
            tick_store.write_health(
                session_date,
                TickHealthEvent(connected=False, at_utc=now, detail=result.stopped_reason).model_dump(
                    mode="json"
                ),
            )
        for event in result.ticks:
            _apply_depth_hint(repo_root, depth_cache)
            payload = event.payload
            received = event.receive_time
            tick = _tick_from_canonical(payload, symbol=symbol, received_at=received)
            signed = sign_tick(
                tick,
                bid_price_1=depth_cache.get("bid"),
                ask_price_1=depth_cache.get("ask"),
                depth_as_of_utc=depth_cache.get("as_of"),
                depth_max_age_ms=pipeline.quality.max_clock_drift_ms,
                now=received,
            )
            tick_store.append_tick(signed, session_date=session_date)
            delta.on_tick(signed, now=received)
            finished = bars.ingest(signed)
            if finished is not None:
                tick_store.append_bar(finished, session_date=session_date)
        tick_store.flush_ticks(session_date=session_date)
        tick_store.flush_bars(session_date=session_date)
        save_delta_state(repo_root, delta.state)
