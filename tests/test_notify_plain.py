"""Plain-language Telegram notify formatting."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import tests.factories as f
from trading.domain.enums import OrderState, Side
from trading.runtime.notify import (
    format_eod_report,
    format_post_trade,
    _human_symbol,
)
from trading.runtime.paper_runner import PaperStrategyOutcome


def test_human_symbol_nifty_option() -> None:
    assert _human_symbol("NSE:NIFTY25102824500CE") == "Nifty 24500 call"


def test_open_fill_line() -> None:
    command = f.order_command(
        side=Side.BUY,
        contract=f.option_contract(symbol="NSE:NIFTY25102824500CE"),
    )
    event = f.order_event(
        state=OrderState.FILLED,
        command=command,
        filled_quantity=75,
        average_fill_price=f.price("120"),
    )
    outcome = PaperStrategyOutcome(
        strategy_id="m2",
        snapshot_id="S1",
        intents=(),
        rejection_reasons=(),
        decisions=(),
        order_events=(event,),
    )
    lines = format_post_trade(outcome, experiment_id="EXP")
    assert lines == ("Bought Nifty 24500 call at 120",)


def test_eod_empty() -> None:
    text = format_eod_report((), open_trade_count=0, charges_verified=False)
    assert text == "Day done: no trades today."
