"""Tests for real-time sub-second stop sentinel."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tests.factories import exit_policy, intent, position_state
from trading.domain.contracts.intent import TradeIntent
from trading.domain.enums import ExitScope, Side, TradeState
from trading.domain.primitives import Price, TickSize
from trading.trade.exits import ExitEvaluation, ExitKind
from trading.trade.sentinel import SentinelEvent, StopSentinel

NOW = datetime(2026, 9, 20, 10, 0, tzinfo=UTC)
TICK = TickSize(Decimal("0.05"))


def _p(val: str) -> Price:
    return Price.snap(val, TICK)


def _long_intent(symbol: str = "NSE:NIFTY2692425000CE") -> TradeIntent:
    ti = intent()
    leg = ti.legs[0]
    return ti.model_copy(
        update={
            "legs": (
                leg.model_copy(
                    update={
                        "side": Side.BUY,
                        "contract": leg.contract.model_copy(
                            update={"trading_symbol": symbol}
                        ),
                    }
                ),
            )
        }
    )


def test_sentinel_registration_and_unregistration() -> None:
    sentinel = StopSentinel()
    intent_obj = _long_intent()
    tid = "trade-1"
    pos = position_state(
        trade_id=tid,
        state=TradeState.OPEN,
        exit_policy=exit_policy(
            trade_id=tid,
            scope=ExitScope.LEG_PRICE,
            stop_price=_p("100"),
            target_price=_p("200"),
        ),
    )

    assert sentinel.register_position(pos, intent_obj) is True
    assert sentinel.monitored_trade_count == 1
    assert "NSE:NIFTY2692425000CE" in sentinel.monitored_symbols

    # Registering closed trade fails
    closed_pos = pos.model_copy(update={"state": TradeState.CLOSED})
    assert sentinel.register_position(closed_pos, intent_obj) is False
    assert sentinel.monitored_trade_count == 1

    # Unregister removes trade and symbol
    sentinel.unregister_position(tid)
    assert sentinel.monitored_trade_count == 0
    assert sentinel.monitored_symbols == ()


def test_sentinel_long_stop_trigger() -> None:
    events: list[SentinelEvent] = []

    def on_exit(
        trade_id: str, eval_: ExitEvaluation, trigger: Price, ts: datetime
    ) -> None:
        events.append(
            SentinelEvent(
                trade_id=trade_id,
                symbol="NSE:NIFTY2692425000CE",
                trigger_price=trigger,
                evaluation=eval_,
                timestamp=ts,
            )
        )

    sentinel = StopSentinel(on_exit=on_exit)
    intent_obj = _long_intent()
    tid = "trade-long-1"
    pos = position_state(
        trade_id=tid,
        state=TradeState.OPEN,
        exit_policy=exit_policy(
            trade_id=tid,
            scope=ExitScope.LEG_PRICE,
            stop_price=_p("95"),
            target_price=_p("150"),
        ),
    )
    sentinel.register_position(pos, intent_obj)

    # Tick 1: Above stop -> no trigger
    no_events = sentinel.on_tick(
        "NSE:NIFTY2692425000CE",
        ltp=_p("105"),
        bid=_p("104"),
        ask=_p("106"),
        timestamp=NOW,
    )
    assert len(no_events) == 0
    assert len(events) == 0

    # Tick 2: Bid drops to 94.5 (below 95 stop) -> fires STOP immediately
    stop_events = sentinel.on_tick(
        "NSE:NIFTY2692425000CE",
        ltp=_p("95"),
        bid=_p("94.5"),
        ask=_p("96"),
        timestamp=NOW + timedelta(seconds=1),
    )
    assert len(stop_events) == 1
    assert stop_events[0].trade_id == tid
    assert stop_events[0].evaluation.kind is ExitKind.STOP
    assert stop_events[0].trigger_price == _p("94.5")
    assert len(events) == 1

    # Position is automatically deregistered after trigger
    assert sentinel.monitored_trade_count == 0


def test_sentinel_target_trigger() -> None:
    sentinel = StopSentinel()
    intent_obj = _long_intent()
    tid = "trade-target-1"
    pos = position_state(
        trade_id=tid,
        state=TradeState.OPEN,
        exit_policy=exit_policy(
            trade_id=tid,
            scope=ExitScope.LEG_PRICE,
            stop_price=_p("90"),
            target_price=_p("120"),
        ),
    )
    sentinel.register_position(pos, intent_obj)

    # Tick reaches 121 bid -> fires TARGET
    fired = sentinel.on_tick(
        "NSE:NIFTY2692425000CE",
        ltp=_p("122"),
        bid=_p("121"),
        ask=_p("123"),
        timestamp=NOW,
    )
    assert len(fired) == 1
    assert fired[0].evaluation.kind is ExitKind.TARGET
    assert fired[0].trigger_price == _p("121")


def test_sentinel_short_position_breach() -> None:
    sentinel = StopSentinel()
    intent_obj = intent()
    leg = intent_obj.legs[0].model_copy(update={"side": Side.SELL})
    intent_obj = intent_obj.model_copy(update={"legs": (leg,)})

    tid = "trade-short-1"
    pos = position_state(
        trade_id=tid,
        state=TradeState.OPEN,
        exit_policy=exit_policy(
            trade_id=tid,
            scope=ExitScope.LEG_PRICE,
            stop_price=_p("110"),
            target_price=_p("50"),
        ),
    )
    sentinel.register_position(pos, intent_obj)

    # For short, exit buys back at ask. Ask jumps to 112 -> stop hit
    fired = sentinel.on_tick(
        leg.contract.symbol,
        ltp=_p("111"),
        bid=_p("110"),
        ask=_p("112"),
        timestamp=NOW,
    )
    assert len(fired) == 1
    assert fired[0].evaluation.kind is ExitKind.STOP
    assert fired[0].trigger_price == _p("112")
