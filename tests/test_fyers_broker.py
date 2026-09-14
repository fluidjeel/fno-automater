"""Fyers broker adapter (L2-015).

Invariant 5: broker-reported funds, positions and orders are external truth.
Invariant 11: duplicate idempotency keys fail closed at the broker boundary.
Invariant 12: UNKNOWN submit outcome blocks replacement until reconciliation.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

import tests.factories as f
from trading.broker.fyers import FyersBroker, FyersBrokerConfig, FyersTransactionClient
from trading.broker.fyers.mapping import map_fyers_order_state, to_fyers_symbol
from trading.broker.ports import (
    BrokerPort,
    BrokerSubmitRequest,
    DuplicateBrokerOrderError,
    MarginPreviewLeg,
    MarginPreviewPort,
    MarginPreviewRequest,
)
from trading.data.settings import FyersSettings
from trading.domain.clock import FrozenClock
from trading.domain.enums import OrderState, Side
from trading.domain.ids import SequentialIdFactory
from trading.domain.primitives import Money

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "broker" / "fyers"
NOW = datetime(2026, 9, 14, 4, 0, tzinfo=UTC)


def _load(name: str) -> dict[str, object]:
    payload: dict[str, object] = json.loads(
        (FIXTURES / name).read_text(encoding="utf-8")
    )
    return payload


def _settings(monkeypatch: pytest.MonkeyPatch) -> FyersSettings:
    monkeypatch.setenv("FYERS_APP_ID", "APP-100")
    monkeypatch.setenv("FYERS_SECRET_KEY", "secret")
    monkeypatch.setenv("FYERS_ACCESS_TOKEN", "token")
    return FyersSettings()  # type: ignore[call-arg]


def _orderbook_fixture(request: httpx.Request) -> httpx.Response:
    order_id = request.url.params.get("id")
    if order_id == "25091400000002":
        return httpx.Response(200, json=_load("orderbook_cancelled.json"))
    return httpx.Response(200, json=_load("orderbook_filled.json"))


def _router() -> httpx.MockTransport:
    """Route Fyers transaction calls to sanitized fixture payloads."""
    routes: dict[tuple[str, str], str | None] = {
        ("/orders/sync", "POST"): "place_order_ok.json",
        ("/orders/sync", "DELETE"): "cancel_ok.json",
        ("/positions", "GET"): "positions.json",
        ("/funds", "GET"): "funds.json",
        ("/multiorder/margin", "POST"): "margin_preview.json",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        for (suffix, method), fixture in routes.items():
            if path.endswith(suffix) and request.method == method:
                assert fixture is not None
                return httpx.Response(200, json=_load(fixture))
        if path.endswith("/orders") and request.method == "GET":
            return _orderbook_fixture(request)
        return httpx.Response(
            404,
            json={"s": "error", "message": f"unmocked {request.method} {path}"},
        )

    return httpx.MockTransport(handler)


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(NOW)


@pytest.fixture
def id_factory(clock: FrozenClock) -> SequentialIdFactory:
    return SequentialIdFactory(clock.instant)


@pytest.fixture
def broker(
    monkeypatch: pytest.MonkeyPatch,
    clock: FrozenClock,
    id_factory: SequentialIdFactory,
) -> FyersBroker:
    settings = _settings(monkeypatch)
    client = FyersTransactionClient(settings, transport=_router())
    config = FyersBrokerConfig(
        account_id="ACC-FYERS-1",
        strategy_id="positional_index_options_poc",
        margin_preview_verified=True,
    )
    return FyersBroker(client, config, clock=clock, id_factory=id_factory)


def _submit_request(**overrides: object) -> BrokerSubmitRequest:
    return BrokerSubmitRequest.model_validate(
        {
            "account_id": "ACC-FYERS-1",
            "strategy_id": "positional_index_options_poc",
            "order": f.planned_order(),
            **overrides,
        }
    )


class TestProtocolSurface:
    def test_broker_exposes_port_protocols(self, broker: FyersBroker) -> None:
        assert isinstance(broker, BrokerPort)
        assert isinstance(broker, MarginPreviewPort)


class TestFundsAndPositions:
    def test_get_funds_maps_broker_truth(self, broker: FyersBroker) -> None:
        """Invariant 5: funds query reflects broker truth."""
        funds = broker.get_funds()
        assert funds.account_id == "ACC-FYERS-1"
        assert funds.equity == f.money("700000")
        assert funds.margin_used == f.money("12000")
        assert funds.margin_available == f.money("688000")

    def test_get_positions_maps_broker_truth(self, broker: FyersBroker) -> None:
        positions = broker.get_positions()
        assert len(positions) == 1
        assert positions[0].quantity_contracts == 75
        assert positions[0].average_price == f.price("120.00")
        assert positions[0].contract.symbol == "NIFTY26SEP24000CE"


class TestOrderLifecycle:
    def test_submit_returns_filled_order_event(self, broker: FyersBroker) -> None:
        event = broker.submit(_submit_request())
        assert event.state is OrderState.FILLED
        assert event.filled_quantity == 75
        assert event.average_fill_price == f.price("120.00")
        assert event.identity.broker_order_id == "25091400000001"

    def test_get_order_round_trips_submitted_event(self, broker: FyersBroker) -> None:
        submitted = broker.submit(_submit_request())
        recovered = broker.get_order("ORD-1")
        assert recovered == submitted

    def test_idempotent_retry_returns_same_event(self, broker: FyersBroker) -> None:
        """Invariant 11: retries with the same idempotency key are stable."""
        first = broker.submit(_submit_request())
        second = broker.submit(_submit_request(attempt_number=2))
        assert first == second
        assert len(broker.list_orders()) == 1

    def test_conflicting_idempotency_key_raises(self, broker: FyersBroker) -> None:
        """Invariant 11: a different order cannot reuse an idempotency key."""
        broker.submit(_submit_request())
        conflicting = _submit_request(
            order=f.planned_order(
                identity=f.order_identity(internal_order_id="ORD-2")
            )
        )
        with pytest.raises(DuplicateBrokerOrderError) as exc_info:
            broker.submit(conflicting)
        assert exc_info.value.existing_event.identity.internal_order_id == "ORD-1"


class TestMarginPreview:
    def test_verified_margin_preview_returns_fixture_result(
        self,
        broker: FyersBroker,
    ) -> None:
        request = MarginPreviewRequest(
            request_id="MARGIN-REQ-1",
            account_id="ACC-FYERS-1",
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

    def test_unverified_margin_fails_closed(
        self,
        monkeypatch: pytest.MonkeyPatch,
        clock: FrozenClock,
        id_factory: SequentialIdFactory,
    ) -> None:
        settings = _settings(monkeypatch)
        client = FyersTransactionClient(settings, transport=_router())
        config = FyersBrokerConfig(
            account_id="ACC-FYERS-1",
            strategy_id="positional_index_options_poc",
            margin_preview_verified=False,
        )
        broker = FyersBroker(client, config, clock=clock, id_factory=id_factory)
        request = MarginPreviewRequest(
            request_id="MARGIN-REQ-UNKNOWN",
            account_id="ACC-FYERS-1",
            legs=(
                MarginPreviewLeg(
                    contract=f.option_contract(),
                    side=Side.BUY,
                    quantity_contracts=75,
                ),
            ),
        )
        result = broker.preview_margin(request)
        assert result.confirmed is False
        assert result.margin_required == Money.zero(f.money("0").currency)


class TestPaperParityDrill:
    def test_submit_then_query_positions_matches_paper_slice(
        self,
        broker: FyersBroker,
    ) -> None:
        """Paper parity drill: entry fill is visible in broker positions."""
        event = broker.submit(_submit_request())
        positions = broker.get_positions()
        funds = broker.get_funds()
        assert event.state is OrderState.FILLED
        assert len(positions) == 1
        assert positions[0].quantity_contracts == 75
        assert funds.margin_used == f.money("12000")


class TestMapping:
    def test_to_fyers_symbol_uses_exchange_prefix(self) -> None:
        contract = f.option_contract()
        assert to_fyers_symbol(contract) == "NSE:NIFTY26SEP24000CE"

    def test_map_status_codes(self) -> None:
        assert (
            map_fyers_order_state(2, filled_qty=75, requested_qty=75)
            is OrderState.FILLED
        )
        assert (
            map_fyers_order_state(6, filled_qty=0, requested_qty=75)
            is OrderState.ACKNOWLEDGED
        )
        assert (
            map_fyers_order_state(5, filled_qty=0, requested_qty=75)
            is OrderState.REJECTED
        )
