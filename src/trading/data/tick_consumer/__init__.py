"""Tick stream pipeline for M2 agentic revamp."""

from trading.data.tick_consumer.bars_1m import BarAggregator, minute_start_ist
from trading.data.tick_consumer.delta import DeltaTracker, apply_signed_tick
from trading.data.tick_consumer.models import (
    Bar1m,
    DeltaState,
    SignedTick,
    TickEvent,
    TickHealthEvent,
    TickSign,
)
from trading.data.tick_consumer.signing import sign_tick
from trading.data.tick_consumer.store import TickStore, bars_parquet_path, tick_parquet_path

__all__ = [
    "Bar1m",
    "BarAggregator",
    "DeltaState",
    "DeltaTracker",
    "SignedTick",
    "TickEvent",
    "TickHealthEvent",
    "TickSign",
    "TickStore",
    "apply_signed_tick",
    "bars_parquet_path",
    "minute_start_ist",
    "tick_parquet_path",
]


def run_tick_multiplex_daemon(*args: object, **kwargs: object) -> None:
    from trading.data.tick_consumer.ws_multiplex import (
        run_tick_multiplex_daemon as _run,
    )

    _run(*args, **kwargs)
