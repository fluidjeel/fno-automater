"""DISC-A3: event blackout and daily-loss freezes soft; drawdown alert."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
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
from tests.test_paper_data_requirements import _inputs as paper_data_inputs
from tests.test_risk_gateway import _gateway_request, instrument_spec
from trading.broker.paper import PaperBroker
from trading.config import load_config, load_risk_policy
from trading.config.paper_data import load_paper_data_requirements
from trading.domain.clock import FrozenClock
from trading.domain.contracts.mode_policy import load_modes_config
from trading.domain.enums import (
    EntryProfile,
    ModeId,
    ReasonCode,
    RiskAction,
    Trigger,
)
from trading.domain.ids import SequentialIdFactory
from trading.domain.primitives import Currency, Money
from trading.risk import CapitalReservationService, RiskGateway
from trading.risk.gateway import RiskGatewayRequest
from trading.risk.mode_ledger import FourModeBook, ModeLedger
from trading.runtime.discovery_drawdown_alert import DrawdownAlertTracker
from trading.safety.controls import SafetyControls
from trading.safety.paper_data import assess_paper_data
from trading.storage.trading_store import TradingStore

ROOT = Path(__file__).resolve().parent.parent
ACCOUNT_CONFIG = load_config(ROOT / "config" / "base.yaml")
RISK_POLICY = load_risk_policy(ROOT / "config" / "risk.yaml")
PAPER_DATA = load_paper_data_requirements(ROOT / "config" / "paper_data.yaml")
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
    return TradingStore.open(tmp_path / "trading.db", clock=clock)


@pytest.fixture
def broker(clock: FrozenClock, id_factory: SequentialIdFactory) -> PaperBroker:
    return PaperBroker.from_fixtures(
        BROKER_FIXTURES,
        clock=clock,
        id_factory=id_factory,
    )


@pytest.fixture
def strict_gateway(
    store: TradingStore,
    broker: PaperBroker,
    clock: FrozenClock,
    id_factory: SequentialIdFactory,
) -> RiskGateway:
    return RiskGateway(
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


class TestEventBlackoutSoft:
    def test_discovery_approves_with_event_blackout_shadow(
        self,
        store: TradingStore,
        broker: PaperBroker,
        clock: FrozenClock,
        id_factory: SequentialIdFactory,
    ) -> None:
        """BLOCK_NEW_ENTRIES under DISCOVERY approves; strict_would_block records it."""
        gateway = _discovery_gateway(store, broker, clock, id_factory)
        legs = _straddle_leg_snapshots()
        intent = _straddle_intent(snapshot_id=legs["call"].snapshot_id)
        decision = gateway.evaluate(
            RiskGatewayRequest(
                intent=intent,
                feature_snapshot=legs["call"],
                portfolio_snapshot=f.portfolio_snapshot(),
                instrument=instrument_spec(),
                leg_snapshots=legs,
                event_risk_state=f.event_risk_state(state="BLOCK_NEW_ENTRIES"),
            )
        )
        assert decision.action in {RiskAction.APPROVE, RiskAction.RESIZE}
        assert ReasonCode.EVENT_BLACKOUT in decision.strict_would_block

    def test_strict_rejects_event_blackout(
        self,
        strict_gateway: RiskGateway,
    ) -> None:
        decision = strict_gateway.evaluate(
            replace(
                _gateway_request(),
                event_risk_state=f.event_risk_state(state="BLOCK_NEW_ENTRIES"),
            )
        )
        assert decision.action is RiskAction.REJECT
        assert decision.reason_codes == (ReasonCode.EVENT_BLACKOUT,)
        assert decision.strict_would_block == ()

    def test_paper_p0_event_missing_soft_under_discovery(self) -> None:
        assessment = assess_paper_data(
            PAPER_DATA,
            paper_data_inputs(event_risk=None),
            entry_profile=EntryProfile.DISCOVERY,
            discovery_config=DISCOVERY_CFG,
        )
        assert assessment.p0_ok
        assert ReasonCode.EVENT_BLACKOUT in assessment.p0_reason_codes


class TestModeDailyLossSoft:
    def test_m3_down_five_percent_continues_with_soft_shadow(
        self,
        store: TradingStore,
        broker: PaperBroker,
        clock: FrozenClock,
        id_factory: SequentialIdFactory,
    ) -> None:
        """Mode down 5% today: entries continue; RISK_LIMIT_DAILY_LOSS is soft."""
        book = FourModeBook(discovery_config=DISCOVERY_CFG)
        m4 = book.get_ledger(ModeId.M4_STRATEGIC_POSITIONAL)
        book._ledgers[ModeId.M4_STRATEGIC_POSITIONAL] = m4.model_copy(
            update={"realized_pnl_today": _money("-35000")}
        )
        gateway = _discovery_gateway(store, broker, clock, id_factory, mode_book=book)
        legs = _straddle_leg_snapshots()
        intent = _straddle_intent(
            mode_id=ModeId.M4_STRATEGIC_POSITIONAL,
            snapshot_id=legs["call"].snapshot_id,
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
        assert ReasonCode.RISK_LIMIT_DAILY_LOSS in decision.strict_would_block

    def test_mode_daily_loss_kill_switch_does_not_latch_under_discovery(
        self,
        clock: FrozenClock,
        id_factory: SequentialIdFactory,
    ) -> None:
        controls = SafetyControls(
            clock=clock,
            id_factory=id_factory,
            discovery_config=DISCOVERY_CFG,
        )
        ledger = ModeLedger(
            mode_id=ModeId.M3_TACTICAL_POSITIONAL,
            allocated_capital=_money("700000"),
            realized_pnl_today=_money("-35000"),
            high_water_mark=_money("700000"),
        )
        fraction = MODES.modes[ModeId.M3_TACTICAL_POSITIONAL].daily_budget_cap_fraction
        assert ledger.daily_loss_breached(fraction)
        event = controls.evaluate_mode_daily_loss(
            ModeId.M3_TACTICAL_POSITIONAL,
            ledger,
            scope="test",
            daily_budget_cap_fraction=fraction,
        )
        assert event is None
        assert controls.blocks_entry(mode_id=ModeId.M3_TACTICAL_POSITIONAL) is False


class TestManualKillSwitchHard:
    def test_global_halt_blocks_both_profiles(
        self,
        clock: FrozenClock,
        id_factory: SequentialIdFactory,
    ) -> None:
        """Manual kill switch stays HARD in STRICT and DISCOVERY."""
        controls = SafetyControls(clock=clock, id_factory=id_factory)
        controls.activate_global_halt(
            actor="operator",
            scope="test",
            trigger=Trigger.OPERATOR,
        )
        assert controls.blocks_entry()

        discovery_controls = SafetyControls(
            clock=clock,
            id_factory=id_factory,
            discovery_config=DISCOVERY_CFG,
        )
        discovery_controls.activate_global_halt(
            actor="operator",
            scope="test",
            trigger=Trigger.OPERATOR,
        )
        assert discovery_controls.blocks_entry()


class TestDrawdownAlert:
    def test_m3_equity_sends_one_telegram_per_day(self) -> None:
        """M3 equity ₹5,55,000 triggers exactly one drawdown alert that day."""
        book = FourModeBook(discovery_config=DISCOVERY_CFG)
        m3 = book.get_ledger(ModeId.M3_TACTICAL_POSITIONAL)
        book._ledgers[ModeId.M3_TACTICAL_POSITIONAL] = m3.model_copy(
            update={"realized_pnl_today": _money("-145000")}
        )
        assert book.get_ledger(ModeId.M3_TACTICAL_POSITIONAL).equity == _money("555000")
        sent: list[str] = []

        def notify(text: str) -> bool:
            sent.append(text)
            return True

        tracker = DrawdownAlertTracker()
        session_day = date(2026, 9, 26)
        first = tracker.check_and_notify(
            book,
            DISCOVERY_CFG,
            session_date=session_day,
            notify=notify,
        )
        second = tracker.check_and_notify(
            book,
            DISCOVERY_CFG,
            session_date=session_day,
            notify=notify,
        )
        assert first == (ModeId.M3_TACTICAL_POSITIONAL,)
        assert second == ()
        assert len(sent) == 1
        assert "M3_TACTICAL_POSITIONAL" in sent[0]
        assert "555" in sent[0].replace(",", "")
