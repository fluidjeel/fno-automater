"""DISC-A2: discovery sizing, bug guard, and per-mode open-risk cap."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from threading import Thread

import pytest

import tests.factories as f
from tests.test_risk_gateway import instrument_spec
from trading.broker.paper import PaperBroker
from trading.config import load_config, load_risk_policy
from trading.config.discovery import load_discovery_config
from trading.domain.clock import FrozenClock
from trading.domain.contracts import DerivativesContext, FeatureSnapshot, IntentLeg
from trading.domain.contracts.intent import TradeIntent
from trading.domain.contracts.risk import RiskDecision
from trading.domain.enums import (
    FamilyId,
    ModeId,
    OptionType,
    ReasonCode,
    RiskAction,
    Side,
)
from trading.domain.ids import SequentialIdFactory
from trading.domain.primitives import Currency, Money
from trading.risk.discovery_sizing import (
    apply_discovery_lots,
    discovery_bug_guard_cap,
    discovery_guide_budget,
    discovery_open_risk_cap,
    evaluate_discovery_hard_limits,
)
from trading.risk.gateway import RiskGateway, RiskGatewayRequest
from trading.risk.mode_ledger import FourModeBook, ModeLedger
from trading.risk.reservation import CapitalReservationService
from trading.storage.trading_store import TradingStore

ROOT = Path(__file__).resolve().parent.parent
DISCOVERY_CFG = load_discovery_config(ROOT / "config" / "discovery.yaml").config
RISK_POLICY = load_risk_policy(ROOT / "config" / "risk.yaml")
ACCOUNT_CONFIG = load_config(ROOT / "config" / "base.yaml")
BROKER_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "broker"
NOW = f.NOW


def _money(val: str) -> Money:
    return Money.of(val, Currency.INR)


def _m4_ledger() -> ModeLedger:
    return ModeLedger(
        mode_id=ModeId.M4_STRATEGIC_POSITIONAL,
        allocated_capital=_money("700000"),
        high_water_mark=_money("700000"),
    )


def _discovery_gateway(
    store: TradingStore,
    broker: PaperBroker,
    clock: FrozenClock,
    id_factory: SequentialIdFactory,
    *,
    mode_book: FourModeBook | None = None,
) -> RiskGateway:
    book = mode_book or FourModeBook(discovery_config=DISCOVERY_CFG)
    return RiskGateway(
        account_config=ACCOUNT_CONFIG,
        risk_policy=RISK_POLICY,
        reservation_service=CapitalReservationService(
            store, clock=clock, id_factory=id_factory
        ),
        margin_preview=broker,
        clock=clock,
        id_factory=id_factory,
        mode_book=book,
    )


def _straddle_leg_snapshots(
    *,
    ask: str = "160.00",
) -> dict[str, FeatureSnapshot]:
    call = f.option_contract(symbol="NIFTY26SEP24000CE", strike=Decimal("24000"))
    put = f.option_contract(
        symbol="NIFTY26SEP24000PE",
        strike=Decimal("24000"),
        option_type=OptionType.PUT,
    )
    bid = str(Decimal(ask) - Decimal("1"))
    return {
        "call": f.snapshot(
            contract=call,
            market=f.quote(
                bid=f.price(bid),
                ask=f.price(ask),
                bid_size=500,
                ask_size=500,
            ),
            derivatives=DerivativesContext(
                days_to_expiry=10,
                open_interest=5000,
                option_type=OptionType.CALL,
                underlying_price=f.price("24000"),
            ),
        ),
        "put": f.snapshot(
            contract=put,
            market=f.quote(
                bid=f.price(bid),
                ask=f.price(ask),
                bid_size=500,
                ask_size=500,
            ),
            derivatives=DerivativesContext(
                days_to_expiry=10,
                open_interest=5000,
                option_type=OptionType.PUT,
                underlying_price=f.price("24000"),
            ),
        ),
    }


def _straddle_intent(**overrides: object) -> TradeIntent:
    legs = _straddle_leg_snapshots()
    payload = {
        "mode_id": ModeId.M4_STRATEGIC_POSITIONAL,
        "family_id": FamilyId.long_straddle.value,
        "snapshot_id": legs["call"].snapshot_id,
        "requested_risk": _money("25000"),
        "estimated_max_loss": _money("30000"),
        "legs": (
            IntentLeg(
                leg_id="call", contract=legs["call"].contract, side=Side.BUY, ratio=1
            ),
            IntentLeg(
                leg_id="put", contract=legs["put"].contract, side=Side.BUY, ratio=1
            ),
        ),
    }
    payload.update(overrides)
    return f.intent(**payload)


class TestDiscoverySizingFormula:
    def test_m4_iron_condor_three_lots_from_guide(self) -> None:
        """M4 ₹7L, guide 3% = ₹21k, one-lot loss ₹6k → 3 lots."""
        ledger = _m4_ledger()
        guide = discovery_guide_budget(
            ledger, DISCOVERY_CFG, ModeId.M4_STRATEGIC_POSITIONAL
        )
        assert guide == _money("21000")
        result = apply_discovery_lots(cost_per_lot=_money("6000"), guide=guide)
        assert result.approved_lots == 3
        assert result.recalculated_max_loss == _money("18000")
        assert result.one_lot_over_guide is False

    def test_one_lot_over_guide_when_cost_exceeds_guide(self) -> None:
        ledger = _m4_ledger()
        guide = discovery_guide_budget(
            ledger, DISCOVERY_CFG, ModeId.M4_STRATEGIC_POSITIONAL
        )
        result = apply_discovery_lots(cost_per_lot=_money("24000"), guide=guide)
        assert result.approved_lots == 1
        assert result.one_lot_over_guide is True

    def test_bug_guard_rejects_seventy_five_thousand_one_lot(self) -> None:
        ledger = _m4_ledger()
        bug_cap = discovery_bug_guard_cap(ledger, DISCOVERY_CFG)
        assert bug_cap == _money("70000")
        check = evaluate_discovery_hard_limits(
            cost_per_lot=_money("75000"),
            recalculated_max_loss=_money("75000"),
            approved_lots=1,
            mode_ledger=ledger,
            discovery=DISCOVERY_CFG,
            mode_id=ModeId.M4_STRATEGIC_POSITIONAL,
        )
        assert not check.passed
        assert ReasonCode.RISK_LIMIT_TRADE in check.reason_codes
        assert "bug_guard_trade_risk_fraction" in check.applied_limits

    def test_m2_open_risk_cap_rejects_while_m3_unaffected(self) -> None:
        ledger_m2 = ModeLedger(
            mode_id=ModeId.M2_DIRECTIONAL,
            allocated_capital=_money("700000"),
            margin_used=_money("80000"),
            high_water_mark=_money("700000"),
        )
        open_cap = discovery_open_risk_cap(
            ledger_m2, DISCOVERY_CFG, ModeId.M2_DIRECTIONAL
        )
        assert open_cap == _money("84000")
        check = evaluate_discovery_hard_limits(
            cost_per_lot=_money("10000"),
            recalculated_max_loss=_money("10000"),
            approved_lots=1,
            mode_ledger=ledger_m2,
            discovery=DISCOVERY_CFG,
            mode_id=ModeId.M2_DIRECTIONAL,
        )
        assert not check.passed
        assert ReasonCode.RISK_LIMIT_PORTFOLIO in check.reason_codes

        ledger_m3 = ModeLedger(
            mode_id=ModeId.M3_TACTICAL_POSITIONAL,
            allocated_capital=_money("700000"),
            high_water_mark=_money("700000"),
        )
        check_m3 = evaluate_discovery_hard_limits(
            cost_per_lot=_money("10000"),
            recalculated_max_loss=_money("10000"),
            approved_lots=1,
            mode_ledger=ledger_m3,
            discovery=DISCOVERY_CFG,
            mode_id=ModeId.M3_TACTICAL_POSITIONAL,
        )
        assert check_m3.passed


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


class TestDiscoveryGateway:
    def test_straddle_one_lot_over_guide_with_strict_shadow(
        self,
        store: TradingStore,
        broker: PaperBroker,
        clock: FrozenClock,
        id_factory: SequentialIdFactory,
    ) -> None:
        """Long straddle over guide → 1 lot, ONE_LOT_OVER_GUIDE, strict MIN_LOT shadow."""
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
                event_risk_state=f.event_risk_state(),
            )
        )
        assert decision.action in {RiskAction.APPROVE, RiskAction.RESIZE}
        assert decision.approved_legs
        assert decision.approved_legs[0].lots.count == 1
        assert ReasonCode.ONE_LOT_OVER_GUIDE in decision.reason_codes
        assert ReasonCode.MIN_LOT_EXCEEDS_BUDGET in decision.strict_would_block

    def test_m2_open_risk_rejected_at_gateway(
        self,
        store: TradingStore,
        broker: PaperBroker,
        clock: FrozenClock,
        id_factory: SequentialIdFactory,
    ) -> None:
        book = FourModeBook(discovery_config=DISCOVERY_CFG)
        m4 = book.get_ledger(ModeId.M4_STRATEGIC_POSITIONAL)
        book._ledgers[ModeId.M4_STRATEGIC_POSITIONAL] = m4.model_copy(
            update={"margin_used": _money("80000")}
        )
        gateway = _discovery_gateway(store, broker, clock, id_factory, mode_book=book)
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
        assert ReasonCode.RISK_LIMIT_PORTFOLIO in decision.reason_codes

    def test_concurrent_reservations_cannot_overspend_mode_cap(
        self,
        store: TradingStore,
        broker: PaperBroker,
        clock: FrozenClock,
        id_factory: SequentialIdFactory,
    ) -> None:
        """Reservation before submit; concurrent proposals respect per-mode open risk."""
        book = FourModeBook(discovery_config=DISCOVERY_CFG)
        gateway = _discovery_gateway(store, broker, clock, id_factory, mode_book=book)
        legs = _straddle_leg_snapshots()
        intent_a = _straddle_intent(
            intent_id="INT-A",
            snapshot_id=legs["call"].snapshot_id,
        )
        intent_b = _straddle_intent(
            intent_id="INT-B",
            snapshot_id=legs["call"].snapshot_id,
        )
        results: list[RiskDecision] = []

        def _evaluate(intent: TradeIntent) -> None:
            results.append(
                gateway.evaluate(
                    RiskGatewayRequest(
                        intent=intent,
                        feature_snapshot=legs["call"],
                        portfolio_snapshot=f.portfolio_snapshot(),
                        instrument=instrument_spec(),
                        leg_snapshots=legs,
                        event_risk_state=f.event_risk_state(),
                    )
                )
            )

        t1 = Thread(target=_evaluate, args=(intent_a,))
        t2 = Thread(target=_evaluate, args=(intent_b,))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        reserved = [
            r
            for r in results
            if r.capital_reservation_id is not None
            and r.action in {RiskAction.APPROVE, RiskAction.RESIZE}
        ]
        assert len(reserved) >= 1
        total_reserved = sum(
            (r.reserved_capital.amount for r in reserved if r.reserved_capital),
            Decimal("0"),
        )
        open_cap = discovery_open_risk_cap(
            book.get_ledger(ModeId.M4_STRATEGIC_POSITIONAL),
            DISCOVERY_CFG,
            ModeId.M4_STRATEGIC_POSITIONAL,
        )
        assert total_reserved <= open_cap.amount

    def test_strict_path_unchanged_without_discovery_book(
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
        assert decision.strict_would_block == ()
        assert ReasonCode.MIN_LOT_EXCEEDS_BUDGET in decision.reason_codes
