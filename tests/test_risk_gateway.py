"""Risk gateway: TradeIntent to RiskDecision (L2-006).

Invariant 4: approvals carry Layer 2 recalculated max loss and margin.
Invariant 14: approvals reserve capital before submission.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

import tests.factories as f
from trading.broker.paper import PaperBroker
from trading.config import load_config, load_risk_policy
from trading.domain.clock import FrozenClock
from trading.domain.contracts import (
    DerivativesContext,
    FeatureSnapshot,
    InstrumentSpec,
    IntentLeg,
    PortfolioSnapshot,
    TradeIntent,
)
from trading.domain.enums import (
    Exchange,
    InstrumentKind,
    OptionType,
    ReasonCode,
    ReservationState,
    RiskAction,
    Side,
)
from trading.domain.ids import SequentialIdFactory
from trading.risk import CapitalReservationService, RiskGateway, RiskGatewayRequest
from trading.storage import TradingStore

ROOT = Path(__file__).resolve().parent.parent
RISK_POLICY = load_risk_policy(ROOT / "config" / "risk.yaml")
ACCOUNT_CONFIG = load_config(ROOT / "config" / "base.yaml")
BROKER_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "broker"
NOW = f.NOW


def instrument_spec(**overrides: object) -> InstrumentSpec:
    payload: dict[str, object] = {
        "trading_symbol": "NSE:NIFTY26SEP24000CE",
        "exchange": Exchange.NFO,
        "segment": "NSE_FO",
        "underlying": "NIFTY",
        "instrument_kind": InstrumentKind.OPTION,
        "provider_token": "tok-1",
        "exchange_token": 1,
        "lot_size": 75,
        "tick_size": Decimal("0.05"),
        "price_precision": 2,
        "expiry": date(2026, 9, 24),
        "strike": Decimal("24000"),
        "option_type": OptionType.CALL,
        "trading_session": "0915-1530",
        "source": "fixture",
        "verified_at": date(2026, 9, 1),
    }
    payload.update(overrides)
    return InstrumentSpec.model_validate(payload)


def option_snapshot(**overrides: object) -> FeatureSnapshot:
    payload: dict[str, object] = {
        "contract": f.option_contract(),
        "market": f.quote(
            bid=f.price("91.95"),
            ask=f.price("92.00"),
            bid_size=300,
            ask_size=300,
        ),
        "derivatives": DerivativesContext(
            days_to_expiry=10,
            open_interest=5000,
            option_type=OptionType.CALL,
            underlying_price=f.price("24000"),
        ),
    }
    payload.update(overrides)
    return f.snapshot(**payload)


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(NOW)


@pytest.fixture
def id_factory(clock: FrozenClock) -> SequentialIdFactory:
    return SequentialIdFactory(clock.instant)


@pytest.fixture
def store(clock: FrozenClock, tmp_path: Path) -> TradingStore:
    return TradingStore.open(tmp_path / "trading.db", clock=clock)


@pytest.fixture
def broker(clock: FrozenClock, id_factory: SequentialIdFactory) -> PaperBroker:
    return PaperBroker.from_fixtures(
        BROKER_FIXTURES,
        clock=clock,
        id_factory=id_factory,
    )


@pytest.fixture
def gateway(
    store: TradingStore,
    broker: PaperBroker,
    clock: FrozenClock,
    id_factory: SequentialIdFactory,
) -> RiskGateway:
    return RiskGateway(
        account_config=ACCOUNT_CONFIG,
        risk_policy=RISK_POLICY,
        reservation_service=CapitalReservationService(
            store,
            clock=clock,
            id_factory=id_factory,
        ),
        margin_preview=broker,
        clock=clock,
        id_factory=id_factory,
    )


def _gateway_request(
    *,
    intent: TradeIntent | None = None,
    feature_snapshot: FeatureSnapshot | None = None,
    portfolio_snapshot: PortfolioSnapshot | None = None,
    instrument: InstrumentSpec | None = None,
) -> RiskGatewayRequest:
    snap = feature_snapshot or option_snapshot()
    resolved_intent = intent or f.intent(
        snapshot_id=snap.snapshot_id,
        requested_risk=f.money("6500"),
        estimated_max_loss=f.money("10000"),
    )
    return RiskGatewayRequest(
        intent=resolved_intent,
        feature_snapshot=snap,
        portfolio_snapshot=portfolio_snapshot or f.portfolio_snapshot(),
        instrument=instrument or instrument_spec(),
        event_risk_state=f.event_risk_state(),
    )


class TestRiskGateway:
    def test_required_event_blackout_state_fails_closed_when_missing(
        self,
        gateway: RiskGateway,
    ) -> None:
        decision = gateway.evaluate(replace(_gateway_request(), event_risk_state=None))
        assert decision.action is RiskAction.REJECT
        assert decision.reason_codes == (ReasonCode.EVENT_BLACKOUT,)

    def test_required_event_blackout_blocks_entry(
        self,
        gateway: RiskGateway,
    ) -> None:
        decision = gateway.evaluate(
            replace(
                _gateway_request(),
                event_risk_state=f.event_risk_state(state="BLOCK_NEW_ENTRIES"),
            )
        )
        assert decision.action is RiskAction.REJECT
        assert decision.reason_codes == (ReasonCode.EVENT_BLACKOUT,)

    @pytest.mark.parametrize(
        "event_overrides",
        [
            {"scope": "BANKNIFTY"},
            {"expires_at": NOW - timedelta(seconds=1)},
            {"quality_state": "STALE"},
            {"state": "MARKET_EMERGENCY"},
        ],
        ids=("wrong-scope", "expired", "stale-quality", "emergency"),
    )
    def test_event_blackout_evidence_must_be_usable(
        self,
        gateway: RiskGateway,
        event_overrides: dict[str, object],
    ) -> None:
        request = replace(
            _gateway_request(),
            event_risk_state=f.event_risk_state(**event_overrides),
        )
        decision = gateway.evaluate(request)
        assert decision.action is RiskAction.REJECT
        assert decision.reason_codes == (ReasonCode.EVENT_BLACKOUT,)

    def test_layer2_rejects_missing_required_open_interest(
        self,
        gateway: RiskGateway,
    ) -> None:
        snap = option_snapshot()
        assert snap.derivatives is not None
        snap = snap.model_copy(
            update={
                "derivatives": snap.derivatives.model_copy(
                    update={"open_interest": None}
                )
            }
        )
        decision = gateway.evaluate(
            _gateway_request(
                feature_snapshot=snap,
                intent=f.intent(
                    snapshot_id=snap.snapshot_id,
                    constraints=f.constraints(min_open_interest=1000),
                ),
            )
        )
        assert decision.action is RiskAction.REJECT
        assert decision.reason_codes == (ReasonCode.DEPTH_INSUFFICIENT,)

    def test_approves_long_call_with_reserved_capital(
        self,
        gateway: RiskGateway,
        store: TradingStore,
    ) -> None:
        """Invariant 4 and 14: approval recalculates loss and reserves capital."""
        decision = gateway.evaluate(_gateway_request())
        assert decision.action is RiskAction.APPROVE
        assert decision.permits_submission
        assert decision.recalculated_max_loss is not None
        assert decision.margin_required is not None
        assert decision.recalculated_max_loss != f.money("10000")
        assert decision.capital_reservation_id is not None
        reservation = store.get_reservation(decision.capital_reservation_id)
        assert reservation is not None
        assert reservation.state is ReservationState.RESERVED
        assert reservation.amount == decision.reserved_capital

    def test_rejects_when_margin_preview_is_unconfirmed(
        self,
        gateway: RiskGateway,
    ) -> None:
        """Unconfirmed broker margin fails closed with MARGIN_INSUFFICIENT."""
        unknown = f.option_contract(symbol="UNKNOWN26SEP24000CE")
        snap = option_snapshot(contract=unknown)
        request = RiskGatewayRequest(
            intent=f.intent(
                snapshot_id=snap.snapshot_id,
                legs=(
                    IntentLeg(
                        leg_id="leg-1",
                        contract=unknown,
                        side=Side.BUY,
                        ratio=1,
                    ),
                ),
            ),
            feature_snapshot=snap,
            portfolio_snapshot=f.portfolio_snapshot(),
            instrument=instrument_spec(trading_symbol="NSE:UNKNOWN26SEP24000CE"),
            event_risk_state=f.event_risk_state(),
        )
        decision = gateway.evaluate(request)
        assert decision.action is RiskAction.REJECT
        assert ReasonCode.MARGIN_INSUFFICIENT in decision.reason_codes
        assert decision.capital_reservation_id is None

    def test_rejects_when_per_trade_loss_cap_is_breached(
        self,
        gateway: RiskGateway,
    ) -> None:
        snap = option_snapshot(
            market=f.quote(
                bid=f.price("200.00"),
                ask=f.price("200.10"),
                bid_size=300,
                ask_size=300,
            ),
        )
        request = RiskGatewayRequest(
            intent=f.intent(
                snapshot_id=snap.snapshot_id,
                requested_risk=f.money("20000"),
                estimated_max_loss=f.money("20000"),
            ),
            feature_snapshot=snap,
            portfolio_snapshot=f.portfolio_snapshot(),
            instrument=instrument_spec(),
            event_risk_state=f.event_risk_state(),
        )
        decision = gateway.evaluate(request)
        assert decision.action is RiskAction.REJECT
        assert ReasonCode.RISK_LIMIT_TRADE in decision.reason_codes

    def test_strategy_confidence_does_not_relax_limits(
        self,
        gateway: RiskGateway,
    ) -> None:
        """High confidence cannot override a binding risk cap."""
        snap = option_snapshot(
            market=f.quote(
                bid=f.price("200.00"),
                ask=f.price("200.10"),
                bid_size=300,
                ask_size=300,
            ),
        )
        low = RiskGatewayRequest(
            intent=f.intent(
                snapshot_id=snap.snapshot_id,
                strategy_confidence=Decimal("0.1"),
                requested_risk=f.money("20000"),
                estimated_max_loss=f.money("20000"),
            ),
            feature_snapshot=snap,
            portfolio_snapshot=f.portfolio_snapshot(),
            instrument=instrument_spec(),
            event_risk_state=f.event_risk_state(),
        )
        high = RiskGatewayRequest(
            intent=f.intent(
                snapshot_id=snap.snapshot_id,
                strategy_confidence=Decimal("0.99"),
                requested_risk=f.money("20000"),
                estimated_max_loss=f.money("20000"),
            ),
            feature_snapshot=snap,
            portfolio_snapshot=f.portfolio_snapshot(),
            instrument=instrument_spec(),
            event_risk_state=f.event_risk_state(),
        )
        low_decision = gateway.evaluate(low)
        high_decision = gateway.evaluate(high)
        assert low_decision.action is RiskAction.REJECT
        assert high_decision.action is RiskAction.REJECT
        assert low_decision.reason_codes == high_decision.reason_codes

    def test_resize_when_approved_risk_is_below_requested(
        self,
        gateway: RiskGateway,
    ) -> None:
        snap = option_snapshot()
        decision = gateway.evaluate(
            _gateway_request(
                feature_snapshot=snap,
                intent=f.intent(
                    snapshot_id=snap.snapshot_id,
                    requested_risk=f.money("20000"),
                    estimated_max_loss=f.money("20000"),
                ),
            )
        )
        assert decision.action is RiskAction.RESIZE
        assert decision.recalculated_max_loss is not None
        assert decision.recalculated_max_loss < f.money("20000")
