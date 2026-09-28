"""DISC-A14: DISCOVERY margin soft-resize and safe projection; STRICT unchanged."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

import tests.factories as f
from tests.test_disc_a2_discovery_sizing import (
    BROKER_FIXTURES,
    DISCOVERY_CFG,
    _discovery_gateway,
    _straddle_intent,
    _straddle_leg_snapshots,
)
from tests.test_risk_gateway import instrument_spec, option_snapshot
from trading.broker.paper import PaperBroker
from trading.config import load_config, load_risk_policy
from trading.domain.clock import FrozenClock
from trading.domain.enums import ReasonCode, RiskAction
from trading.domain.ids import SequentialIdFactory
from trading.domain.primitives import Currency, Money
from trading.risk import CapitalReservationService, RiskGateway
from trading.risk.discovery_sizing import apply_discovery_margin_sizing
from trading.risk.gateway import RiskGatewayRequest
from trading.risk.limits import (
    project_discovery_post_trade_exposure,
    project_post_trade_exposure,
)
from trading.risk.mode_ledger import FourModeBook
from trading.storage.trading_store import TradingStore

ROOT = Path(__file__).resolve().parent.parent
ACCOUNT_CONFIG = load_config(ROOT / "config" / "base.yaml")
RISK_POLICY = load_risk_policy(ROOT / "config" / "risk.yaml")
NOW = f.NOW


def _money(val: str) -> Money:
    return Money.of(val, Currency.INR)


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(NOW)


@pytest.fixture
def id_factory(clock: FrozenClock) -> SequentialIdFactory:
    return SequentialIdFactory(clock.instant)


@pytest.fixture
def store(clock: FrozenClock, tmp_path: Path) -> TradingStore:
    return TradingStore.open(tmp_path / "a14.sqlite", clock=clock)


@pytest.fixture
def broker(clock: FrozenClock, id_factory: SequentialIdFactory) -> PaperBroker:
    return PaperBroker.from_fixtures(
        BROKER_FIXTURES,
        clock=clock,
        id_factory=id_factory,
    )


class TestDiscA14MarginProjection:
    def test_strict_projection_still_allows_negative_margin_available(self) -> None:
        """STRICT path keeps project_post_trade_exposure behavior (invalid snapshot)."""
        portfolio = f.portfolio_snapshot(
            exposure=f.exposure(margin_available=_money("1000"))
        )
        with pytest.raises(ValidationError, match="margin figures must not be negative"):
            project_post_trade_exposure(
                portfolio,
                margin_required=_money("50000"),
                premium_paid=_money("25000"),
                net_delta_delta=50,
            )

    def test_discovery_projection_clamps_negative_margin(self) -> None:
        """DISCOVERY projection never produces invalid ExposureSnapshot."""
        portfolio = f.portfolio_snapshot(
            exposure=f.exposure(margin_available=_money("1000"))
        )
        post_trade, adjustment = project_discovery_post_trade_exposure(
            portfolio,
            margin_required=_money("50000"),
            premium_paid=_money("25000"),
            net_delta_delta=50,
        )
        assert post_trade.margin_available == _money("0")
        assert adjustment is not None
        assert adjustment.oversubscription == _money("49000")
        assert adjustment.round_trip() == adjustment


class TestDiscA14DiscoveryMarginSoft:
    def test_margin_insufficient_soft_resizes_to_one_lot(
        self,
        store: TradingStore,
        broker: PaperBroker,
        clock: FrozenClock,
        id_factory: SequentialIdFactory,
    ) -> None:
        """Broker margin binds → 1 lot APPROVE with strict_would_block shadow."""
        gateway = _discovery_gateway(store, broker, clock, id_factory)
        legs = _straddle_leg_snapshots()
        intent = _straddle_intent(snapshot_id=legs["call"].snapshot_id)
        portfolio = f.portfolio_snapshot(
            exposure=f.exposure(margin_available=_money("1000"))
        )
        decision = gateway.evaluate(
            RiskGatewayRequest(
                intent=intent,
                feature_snapshot=legs["call"],
                portfolio_snapshot=portfolio,
                instrument=instrument_spec(),
                leg_snapshots=legs,
                event_risk_state=f.event_risk_state(),
            )
        )
        assert decision.action in {RiskAction.APPROVE, RiskAction.RESIZE}
        assert decision.approved_legs
        assert decision.approved_legs[0].lots.count >= 1
        assert ReasonCode.MARGIN_INSUFFICIENT in decision.strict_would_block
        assert ReasonCode.MARGIN_OVERSUBSCRIBED in decision.reason_codes
        assert decision.margin_projection_adjustment is not None
        assert decision.post_trade_projection is not None
        assert decision.post_trade_projection.margin_available == _money("0")

    def test_negative_margin_crash_regression(
        self,
        store: TradingStore,
        broker: PaperBroker,
        clock: FrozenClock,
        id_factory: SequentialIdFactory,
    ) -> None:
        """Pre-A14 oversubscribed reservation path crashed on ExposureSnapshot."""
        book = FourModeBook(discovery_config=DISCOVERY_CFG)
        gateway = _discovery_gateway(store, broker, clock, id_factory, mode_book=book)
        legs = _straddle_leg_snapshots(ask="160.00")
        intent = _straddle_intent(
            snapshot_id=legs["call"].snapshot_id,
            requested_risk=_money("90000"),
            estimated_max_loss=_money("90000"),
        )
        portfolio = f.portfolio_snapshot(
            exposure=f.exposure(
                margin_available=_money("500"),
                margin_used=_money("699500"),
            )
        )
        decision = gateway.evaluate(
            RiskGatewayRequest(
                intent=intent,
                feature_snapshot=legs["call"],
                portfolio_snapshot=portfolio,
                instrument=instrument_spec(),
                leg_snapshots=legs,
                event_risk_state=f.event_risk_state(),
            )
        )
        assert decision.action in {RiskAction.APPROVE, RiskAction.RESIZE}
        assert decision.post_trade_projection is not None
        assert not decision.post_trade_projection.margin_available.is_negative

    def test_apply_discovery_margin_sizing_never_rejects(self) -> None:
        result = apply_discovery_margin_sizing(
            approved_lots=3,
            recalculated_max_loss=_money("30000"),
            estimated_margin=_money("90000"),
            cost_per_lot=_money("10000"),
            margin_per_lot=_money("30000"),
            margin_lots=0,
            broker_margin_available=_money("1000"),
        )
        assert result.approved_lots == 1
        assert ReasonCode.MARGIN_INSUFFICIENT in result.strict_would_block
        assert result.margin_oversubscribed


class TestDiscA14StrictUnchanged:
    def test_strict_still_rejects_margin_insufficient(
        self,
        store: TradingStore,
        broker: PaperBroker,
        clock: FrozenClock,
        id_factory: SequentialIdFactory,
    ) -> None:
        gateway = RiskGateway(
            account_config=ACCOUNT_CONFIG,
            risk_policy=RISK_POLICY,
            reservation_service=CapitalReservationService(
                store, clock=clock, id_factory=id_factory
            ),
            margin_preview=broker,
            clock=clock,
            id_factory=id_factory,
            mode_book=FourModeBook(),
        )
        unknown = f.option_contract(symbol="UNKNOWN26SEP24000CE")
        snap = option_snapshot(contract=unknown)
        from trading.domain.contracts import IntentLeg
        from trading.domain.enums import Side

        decision = gateway.evaluate(
            RiskGatewayRequest(
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
        )
        assert decision.action is RiskAction.REJECT
        assert ReasonCode.MARGIN_INSUFFICIENT in decision.reason_codes
        assert decision.strict_would_block == ()
        assert decision.margin_projection_adjustment is None

    def test_strict_negative_projection_still_raises(self) -> None:
        portfolio = f.portfolio_snapshot(
            exposure=f.exposure(margin_available=_money("100"))
        )
        with pytest.raises(ValidationError, match="margin figures must not be negative"):
            project_post_trade_exposure(
                portfolio,
                margin_required=_money("50000"),
                premium_paid=_money("10000"),
                net_delta_delta=1,
            )
