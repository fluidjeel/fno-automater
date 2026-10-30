"""Gate mapping and signing tests for platter revamp."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from trading.data.platter_pipeline.config import load_platter_pipeline_config
from trading.data.tick_consumer.models import TickEvent
from trading.data.tick_consumer.signing import sign_tick
from trading.trade.m2_veto import M2VetoAction
from trading.trade.platter_gate import map_platter_gate


def _platter(*, fail_closed: bool = False, blackout: bool = False) -> dict:
    return {
        "fail_closed": fail_closed,
        "events": {"blackout_active": blackout},
        "mechanical": {"setup_direction": "UP"},
    }


def test_sign_tick_buy_at_ask() -> None:
    now = datetime.now(tz=UTC)
    tick = TickEvent(
        symbol="NSE:NIFTY50-INDEX",
        ltp="100",
        volume=1,
        trade_size=1,
        exchange_ts=now,
        received_at=now,
    )
    signed = sign_tick(
        tick,
        bid_price_1="99",
        ask_price_1="100",
        depth_as_of_utc=now,
        depth_max_age_ms=60_000,
        now=now,
    )
    assert signed.sign.value == "BUY"


def test_paper_free_trade_approves_conflict() -> None:
    config = load_platter_pipeline_config()
    gate = map_platter_gate(_platter(), "SHORT", config=config)
    assert gate.action is M2VetoAction.APPROVE
    assert gate.size_fraction == Decimal("1")
    assert gate.alignment == "conflict"


def test_stale_quote_vetoes() -> None:
    config = load_platter_pipeline_config()
    gate = map_platter_gate(_platter(fail_closed=True), "LONG", config=config)
    assert gate.action is M2VetoAction.VETO


def test_blackout_vetoes() -> None:
    config = load_platter_pipeline_config()
    gate = map_platter_gate(_platter(blackout=True), "LONG", config=config)
    assert gate.action is M2VetoAction.VETO
    assert gate.event_blackout_veto
