"""Paper broker adapter (L2-003).

Invariant 5: broker-reported funds, positions and orders are external truth.
Invariant 11: duplicate idempotency keys fail closed at the broker boundary.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

import tests.factories as f
from trading.broker.paper import PaperBroker, PaperBrokerFixtures
from trading.broker.paper.adapter import margin_preview_key
from trading.broker.ports import (
    BrokerPort,
    BrokerSubmitRequest,
    DuplicateBrokerOrderError,
    MarginPreviewLeg,
    MarginPreviewPort,
    MarginPreviewRequest,
)
from trading.domain.clock import FrozenClock
from trading.domain.contracts import OrderEvent
from trading.domain.enums import InstrumentKind, OrderState, Side
from trading.domain.ids import SequentialIdFactory
from trading.domain.primitives import Money

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "broker"
NOW = datetime(2026, 9, 14, 4, 0, tzinfo=UTC)


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(NOW)


@pytest.fixture
def id_factory(clock: FrozenClock) -> SequentialIdFactory:
    return SequentialIdFactory(clock.instant)


@pytest.fixture
def broker(clock: FrozenClock, id_factory: SequentialIdFactory) -> PaperBroker:
    return PaperBroker.from_fixtures(FIXTURES, clock=clock, id_factory=id_factory)


def _submit_request(**overrides: object) -> BrokerSubmitRequest:
    return BrokerSubmitRequest.model_validate(
        {
            "account_id": "ACC-PAPER-1",
            "strategy_id": "positional_index_options_poc",
            "order": f.planned_order(),
            **overrides,
        }
    )


class TestFixtureRoundTrip:
    def test_fixtures_load_account_positions_and_margin(self) -> None:
        """Offline fixtures deserialize into broker DTOs."""
        fixtures = PaperBrokerFixtures.load(FIXTURES)
        assert fixtures.account.account_id == "ACC-PAPER-1"
        assert fixtures.account.equity == f.money("700000")
        assert fixtures.positions == ()
        key = margin_preview_key("NIFTY26SEP24000CE", Side.BUY, 75)
        preview = fixtures.margin_previews[key]
        assert preview.confirmed is True
        assert preview.margin_required == f.money("12000")

    def test_broker_exposes_port_protocols(self, broker: PaperBroker) -> None:
        assert isinstance(broker, BrokerPort)
        assert isinstance(broker, MarginPreviewPort)


class TestFundsAndPositions:
    def test_get_funds_matches_fixture(self, broker: PaperBroker) -> None:
        """Invariant 5: funds query reflects broker truth."""
        funds = broker.get_funds()
        assert funds.account_id == "ACC-PAPER-1"
        assert funds.equity == f.money("700000")
        assert funds.margin_available == f.money("700000")

    def test_submit_creates_position_and_updates_margin(
        self,
        broker: PaperBroker,
    ) -> None:
        """A filled entry updates positions and margin at the broker."""
        event = broker.submit(_submit_request())
        positions = broker.get_positions()
        funds = broker.get_funds()

        assert event.state is OrderState.FILLED
        assert len(positions) == 1
        assert positions[0].trade_id == "TRD-1"
        assert positions[0].quantity_contracts == 75
        assert positions[0].average_price == f.price("120.00")
        assert funds.margin_used == f.money("12000")
        assert funds.margin_available == f.money("688000")


class TestOrderLifecycle:
    def test_submit_returns_filled_order_event(self, broker: PaperBroker) -> None:
        event = broker.submit(_submit_request())
        assert isinstance(event, OrderEvent)
        assert event.state is OrderState.FILLED
        assert event.filled_quantity == 75
        assert event.average_fill_price == f.price("120.00")
        assert event.identity.broker_order_id is not None

    def test_get_order_round_trips_submitted_event(self, broker: PaperBroker) -> None:
        submitted = broker.submit(_submit_request())
        recovered = broker.get_order("ORD-1")
        assert recovered == submitted

    def test_idempotent_retry_returns_same_event(self, broker: PaperBroker) -> None:
        """Invariant 11: retries with the same idempotency key are stable."""
        first = broker.submit(_submit_request())
        second = broker.submit(_submit_request(attempt_number=2))
        assert first == second
        assert len(broker.list_orders()) == 1

    def test_conflicting_idempotency_key_raises(self, broker: PaperBroker) -> None:
        """Invariant 11: a different order cannot reuse an idempotency key."""
        broker.submit(_submit_request())
        conflicting = _submit_request(
            order=f.planned_order(identity=f.order_identity(internal_order_id="ORD-2"))
        )
        with pytest.raises(DuplicateBrokerOrderError) as exc_info:
            broker.submit(conflicting)
        assert exc_info.value.existing_event.identity.internal_order_id == "ORD-1"

    def test_cancel_terminal_order_fails(self, broker: PaperBroker) -> None:
        broker.submit(_submit_request())
        with pytest.raises(Exception, match="terminal"):
            broker.cancel("ORD-1")

    def test_sell_close_removes_position_and_releases_margin(
        self,
        broker: PaperBroker,
    ) -> None:
        """A full exit sell clears broker exposure for slice-1 close."""
        entry = broker.submit(_submit_request())
        trade_id = entry.identity.trade_id
        exit_order = f.planned_order(
            identity=f.order_identity(
                internal_order_id="ORD-EXIT-1",
                trade_id=trade_id,
                idempotency_key="exit-key-1",
            ),
            command=f.order_command(
                side=Side.SELL,
                limit_price=f.price("118.00"),
            ),
        )
        broker.submit(
            _submit_request(order=exit_order, attempt_number=1),
        )
        assert broker.get_positions() == ()
        assert broker.get_funds().margin_used == f.money("0")


class TestMarginPreview:
    def test_preview_margin_returns_fixture_result(self, broker: PaperBroker) -> None:
        request = MarginPreviewRequest(
            request_id="MARGIN-REQ-1",
            account_id="ACC-PAPER-1",
            legs=(
                MarginPreviewLeg(
                    contract=f.option_contract(),
                    side=Side.BUY,
                    quantity_contracts=75,
                ),
            ),
        )
        result = broker.preview_margin(request)
        assert result.confirmed is True
        assert result.margin_required == f.money("12000")
        assert result.margin_available_after == f.money("688000")

    def test_unknown_preview_fails_closed(self, broker: PaperBroker) -> None:
        request = MarginPreviewRequest(
            request_id="MARGIN-REQ-UNKNOWN",
            account_id="ACC-PAPER-1",
            legs=(
                MarginPreviewLeg(
                    contract=f.option_contract(symbol="UNKNOWN26SEP24000CE"),
                    side=Side.BUY,
                    quantity_contracts=75,
                ),
            ),
        )
        result = broker.preview_margin(request)
        assert result.confirmed is False
        assert result.margin_required == Money.zero(f.money("0").currency)


class TestSyntheticPaperMargin:
    def test_missing_fixture_uses_ask_times_quantity(
        self, clock: FrozenClock, id_factory: SequentialIdFactory
    ) -> None:
        funds = PaperBrokerFixtures.load(FIXTURES).account
        broker = PaperBroker.for_session(
            clock=clock,
            id_factory=id_factory,
            funds=funds,
            future_margin_fraction=Decimal("0.15"),
        )
        contract = f.option_contract(symbol="NSE:NIFTY26SEP24500CE")
        broker.publish_quote(
            contract.symbol,
            f.quote(bid=f.price("90"), ask=f.price("92"), last=f.price("91")),
        )
        result = broker.preview_margin(
            MarginPreviewRequest(
                request_id="MARGIN-LIVE-1",
                account_id="ACC-PAPER-1",
                legs=(
                    MarginPreviewLeg(
                        contract=contract,
                        side=Side.BUY,
                        quantity_contracts=75,
                    ),
                ),
            )
        )
        assert result.confirmed is True
        assert result.margin_required == f.money("6900")

    def test_future_without_fraction_fails_closed(
        self, clock: FrozenClock, id_factory: SequentialIdFactory
    ) -> None:
        funds = PaperBrokerFixtures.load(FIXTURES).account
        broker = PaperBroker.for_session(
            clock=clock, id_factory=id_factory, funds=funds
        )
        contract = f.option_contract(
            symbol="MCX:CRUDEOIL26SEPFUT",
            exchange=f.option_contract().exchange,
        )
        future = contract.model_copy(
            update={
                "instrument_kind": InstrumentKind.FUTURE,
                "option_type": None,
                "strike": None,
                "symbol": "MCX:CRUDEOIL26SEPFUT",
            }
        )
        broker.publish_quote(future.symbol, f.quote(last=f.price("6000")))
        result = broker.preview_margin(
            MarginPreviewRequest(
                request_id="MARGIN-FUT-1",
                account_id="ACC-PAPER-1",
                legs=(
                    MarginPreviewLeg(
                        contract=future, side=Side.BUY, quantity_contracts=1
                    ),
                ),
            )
        )
        assert result.confirmed is False
