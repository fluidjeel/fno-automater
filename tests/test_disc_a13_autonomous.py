"""DISC-A13: DISCOVERY PAPER takes trades through capital caps; STRICT unchanged."""

from __future__ import annotations

from pathlib import Path

import pytest

import tests.factories as f
from tests.test_disc_a2_discovery_sizing import (
    BROKER_FIXTURES,
    DISCOVERY_CFG,
    _discovery_gateway,
    _straddle_intent,
    _straddle_leg_snapshots,
)
from tests.test_risk_gateway import instrument_spec
from trading.broker.paper import PaperBroker
from trading.config import load_config, load_risk_policy
from trading.domain.clock import FrozenClock
from trading.domain.contracts.mode_policy import load_modes_config
from trading.domain.enums import (
    DataQuality,
    ModeId,
    ReasonCode,
    RiskAction,
)
from trading.domain.ids import SequentialIdFactory
from trading.domain.primitives import Currency, Money
from trading.portfolio.arbitration import PortfolioArbiter
from trading.risk import CapitalReservationService, RiskGateway
from trading.risk.gateway import RiskGatewayRequest
from trading.risk.mode_ledger import FourModeBook
from trading.storage.trading_store import TradingStore

ROOT = Path(__file__).resolve().parent.parent
ACCOUNT_CONFIG = load_config(ROOT / "config" / "base.yaml")
RISK_POLICY = load_risk_policy(ROOT / "config" / "risk.yaml")
MODES = load_modes_config(ROOT / "config" / "modes.yaml")
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
    return TradingStore.open(tmp_path / "a13.sqlite", clock=clock)


@pytest.fixture
def broker(clock: FrozenClock, id_factory: SequentialIdFactory) -> PaperBroker:
    return PaperBroker.from_fixtures(
        BROKER_FIXTURES,
        clock=clock,
        id_factory=id_factory,
    )


class TestDiscA13DiscoveryAutonomous:
    def test_all_capital_caps_shadowed_but_trade_approves(
        self,
        store: TradingStore,
        broker: PaperBroker,
        clock: FrozenClock,
        id_factory: SequentialIdFactory,
    ) -> None:
        """Exceeding bug guard, open-risk cap and daily loss still approves in DISCOVERY."""
        book = FourModeBook(discovery_config=DISCOVERY_CFG)
        m4 = book.get_ledger(ModeId.M4_STRATEGIC_POSITIONAL)
        book._ledgers[ModeId.M4_STRATEGIC_POSITIONAL] = m4.model_copy(
            update={
                "margin_used": _money("80000"),
                "realized_pnl_today": _money("-40000"),
            }
        )
        gateway = _discovery_gateway(store, broker, clock, id_factory, mode_book=book)
        legs = _straddle_leg_snapshots(ask="160.00")
        intent = _straddle_intent(
            snapshot_id=legs["call"].snapshot_id,
            requested_risk=_money("90000"),
            estimated_max_loss=_money("90000"),
        )
        decision = gateway.evaluate(
            RiskGatewayRequest(
                intent=intent,
                feature_snapshot=legs["call"],
                portfolio_snapshot=f.portfolio_snapshot(),
                instrument=instrument_spec(),
                leg_snapshots=legs,
                event_risk_state=f.event_risk_state(),
            )
        )
        assert decision.action in {RiskAction.APPROVE, RiskAction.RESIZE}
        assert decision.approved_legs
        assert decision.approved_legs[0].lots.count >= 1
        assert ReasonCode.RISK_LIMIT_PORTFOLIO in decision.strict_would_block
        assert ReasonCode.RISK_LIMIT_DAILY_LOSS in decision.strict_would_block

    def test_stale_quote_still_rejects_in_discovery(
        self,
        store: TradingStore,
        broker: PaperBroker,
        clock: FrozenClock,
        id_factory: SequentialIdFactory,
    ) -> None:
        gateway = _discovery_gateway(store, broker, clock, id_factory)
        legs = _straddle_leg_snapshots()
        stale_call = legs["call"].model_copy(
            update={
                "quality": f.quality(
                    state=DataQuality.STALE,
                    reason_codes=(ReasonCode.DATA_STALE,),
                )
            }
        )
        legs = {"call": stale_call, "put": legs["put"]}
        intent = _straddle_intent(snapshot_id=stale_call.snapshot_id)
        decision = gateway.evaluate(
            RiskGatewayRequest(
                intent=intent,
                feature_snapshot=stale_call,
                portfolio_snapshot=f.portfolio_snapshot(),
                instrument=instrument_spec(),
                leg_snapshots=legs,
                event_risk_state=f.event_risk_state(),
            )
        )
        assert decision.action is RiskAction.REJECT
        assert ReasonCode.DATA_STALE in decision.reason_codes

    def test_strict_still_rejects_min_lot_budget(
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
        legs = _straddle_leg_snapshots()
        intent = _straddle_intent(snapshot_id=legs["call"].snapshot_id)
        decision = gateway.evaluate(
            RiskGatewayRequest(
                intent=intent,
                feature_snapshot=legs["call"],
                portfolio_snapshot=f.portfolio_snapshot(),
                instrument=instrument_spec(),
                leg_snapshots=legs,
                event_risk_state=f.event_risk_state(),
            )
        )
        assert decision.action is RiskAction.REJECT
        assert ReasonCode.MIN_LOT_EXCEEDS_BUDGET in decision.reason_codes
        assert decision.strict_would_block == ()


class TestDiscA13StrictArbitrationCaps:
    def test_strict_arbiter_has_no_discovery_daily_cap_path(self) -> None:
        from tests.test_disc_a8_mode_arbitration import (
            _m2_long_call,
            _m3_bull_call_spread,
        )
        from trading.domain.enums import EntryProfile

        arbiter = PortfolioArbiter(
            max_m4_open_positions=2,
            entry_profile=EntryProfile.STRICT,
        )
        result = arbiter.arbitrate(
            [_m2_long_call("INTENT-M2-5"), _m3_bull_call_spread("INTENT-M3-OK")],
            now=NOW,
            mode_daily_entries={ModeId.M2_DIRECTIONAL: 4},
        )
        assert {intent.intent_id for intent in result.approved_intents} == {
            "INTENT-M2-5",
            "INTENT-M3-OK",
        }
        assert not result.soft_warnings
