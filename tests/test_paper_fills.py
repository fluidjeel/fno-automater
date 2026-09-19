"""PAPER-002: live paper path uses the conservative fill calculator."""

from __future__ import annotations

from pathlib import Path

import tests.factories as f
from tests.test_l4_fills import verified_fill_model
from trading.broker.paper import PaperBroker
from trading.broker.ports import BrokerSubmitRequest
from trading.config import load_evaluation_config
from trading.domain.clock import FrozenClock
from trading.domain.contracts.order_plan import PlannedOrder
from trading.domain.enums import OrderPlanState, OrderState, ReasonCode, Side
from trading.domain.ids import SequentialIdFactory
from trading.domain.primitives import Price

BROKER_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "broker"
ROOT = Path(__file__).resolve().parent.parent


def _planned(limit: str = "100.05", quantity: int = 75) -> PlannedOrder:
    identity = f.order_event().identity
    return PlannedOrder(
        plan_leg_id="leg-1-entry",
        leg_id="leg-1",
        identity=identity,
        command=f.order_command(
            side=Side.BUY, quantity_contracts=quantity, limit_price=f.price(limit)
        ),
        plan_state=OrderPlanState.RISK_APPROVED,
    )


def test_conservative_paper_fill_uses_ask_plus_slippage() -> None:
    clock = FrozenClock(f.NOW)
    broker = PaperBroker.from_fixtures(
        BROKER_FIXTURES,
        clock=clock,
        id_factory=SequentialIdFactory(clock.instant),
        fill_model=verified_fill_model(),
    )
    command = _planned().command
    broker.publish_quote(
        command.contract.symbol,
        f.quote(bid_size=300, ask_size=300, last=f.price("100.05")),
    )
    event = broker.submit(
        BrokerSubmitRequest(
            account_id="ACC-PAPER-1",
            strategy_id="positional_long_option",
            order=_planned(),
        )
    )
    assert event.state is OrderState.FILLED
    assert event.average_fill_price == Price.snap("100.10", f.TICK)


def test_conservative_paper_does_not_fill_without_trade_through() -> None:
    clock = FrozenClock(f.NOW)
    broker = PaperBroker.from_fixtures(
        BROKER_FIXTURES,
        clock=clock,
        id_factory=SequentialIdFactory(clock.instant),
        fill_model=load_evaluation_config(
            ROOT / "config" / "evaluation.yaml"
        ).config.fill_model,
    )
    planned = _planned(limit="92.00")
    broker.publish_quote(
        planned.command.contract.symbol,
        f.quote(
            bid=f.price("91.95"),
            ask=f.price("92.00"),
            last=f.price("93.00"),
            bid_size=300,
            ask_size=300,
        ),
    )
    event = broker.submit(
        BrokerSubmitRequest(
            account_id="ACC-PAPER-1",
            strategy_id="positional_long_option",
            order=planned,
        )
    )
    assert event.state is OrderState.REJECTED
    assert event.reason_code is ReasonCode.SLIPPAGE_EXCEEDED
    assert event.filled_quantity == 0
    assert broker.get_positions() == ()


def test_immediate_fill_path_unchanged_without_fill_model() -> None:
    clock = FrozenClock(f.NOW)
    broker = PaperBroker.from_fixtures(
        BROKER_FIXTURES,
        clock=clock,
        id_factory=SequentialIdFactory(clock.instant),
    )
    event = broker.submit(
        BrokerSubmitRequest(
            account_id="ACC-PAPER-1",
            strategy_id="positional_long_option",
            order=_planned(),
        )
    )
    assert event.state is OrderState.FILLED
    assert event.average_fill_price == f.price("100.05")
