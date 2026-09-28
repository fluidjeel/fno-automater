"""DISC-A26: DISCOVERY structure checks use REST protection + remaining legs."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

import tests.factories as f
from tests.test_disc_a21_discovery_exits import (
    CYCLE,
    DISCOVERY,
    EXITS,
    NOW,
    _mid_snapshot,
)
from tests.test_disc_a25_rest_protection_structure import (
    _discovery_runner,
    _following_week_legs,
    _rest_quote,
)
from tests.test_paper_runner import BROKER_FIXTURES, ROOT, _paper_config
from trading.broker.paper import PaperBroker
from trading.config import load_risk_policy
from trading.domain.clock import FrozenClock
from trading.domain.contracts import IntentLeg, PositionLifecycleRecord, TradeIntent
from trading.domain.contracts.position import PositionLegState
from trading.domain.enums import (
    ExitScope,
    HoldingStyle,
    ModeId,
    OptionType,
    OrderState,
    QuoteMonitorSource,
    ReasonCode,
    ReviewSlotId,
    Side,
    TradeState,
)
from trading.domain.ids import SequentialIdFactory
from trading.runtime.paper_runner import PaperRunner, _lifecycle_issues
from trading.runtime.review_schedule import ReviewSlot, parse_hhmm
from trading.storage.trading_store import TradingStore
from trading.trade.discovery_exits import (
    apply_discovery_leg_exit_prices,
    build_discovery_exit_policy,
)
from trading.trade.exits import build_exit_policy

SESSION_DATE = date(2026, 9, 14)
MORNING_SLOT = ReviewSlot(ReviewSlotId.NSE_MORNING, parse_hhmm("10:30"))


def _discovery_runner_with_broker(
    store: TradingStore, clock: FrozenClock, broker: PaperBroker
) -> PaperRunner:
    ids = SequentialIdFactory(clock.instant)
    return PaperRunner(
        account_config=_paper_config(),  # type: ignore[arg-type]
        risk_policy=load_risk_policy(ROOT / "config" / "risk.yaml"),
        store=store,
        broker=broker,
        clock=clock,
        id_factory=ids,
        discovery_config=DISCOVERY,
    )


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(CYCLE)


@pytest.fixture
def store(tmp_path: Path, clock: FrozenClock) -> Iterator[TradingStore]:
    trading_store = TradingStore.open(tmp_path / "paper.sqlite", clock=clock)
    yield trading_store
    trading_store.close()


def _positional_strangle_intent(legs: tuple[PositionLegState, ...]) -> TradeIntent:
    return f.intent(
        legs=(
            IntentLeg(leg_id="put", contract=legs[0].contract, side=Side.BUY, ratio=1),
            IntentLeg(leg_id="call", contract=legs[1].contract, side=Side.BUY, ratio=1),
        ),
        mode_id=ModeId.M3_TACTICAL_POSITIONAL,
    )


def _open_positional_strangle(
    runner: PaperRunner,
    store: TradingStore,
    legs: tuple[PositionLegState, ...],
    *,
    trade_id: str = "TRD-A26",
) -> None:
    legs = apply_discovery_leg_exit_prices(legs, EXITS)
    intent = _positional_strangle_intent(legs)
    policy = build_discovery_exit_policy(
        intent,
        legs,
        f.exit_template(stop_distance_ticks=40),
        trade_id=trade_id,
        policy_id=f"EXIT-{trade_id}",
        initialized_at=NOW,
        config=EXITS,
        scope=ExitScope.STRATEGY_PNL,
    )
    position = f.position_state(
        trade_id=trade_id,
        state=TradeState.OPEN,
        legs=legs,
        exit_policy=policy,
        protective_order_ids=("PROT-1",),
        opened_at=NOW,
        mode_id=ModeId.M3_TACTICAL_POSITIONAL,
    )
    store.upsert_position_lifecycle(
        PositionLifecycleRecord(
            trade_id=position.trade_id,
            position=position,
            intent=intent,
            risk_decision=f.risk_decision(intent_id=intent.intent_id),
            holding_style=HoldingStyle.POSITIONAL,
            as_of=position.as_of,
        ),
        event_id=f"PLC-{trade_id}",
    )
    runner.recover_lifecycle()


class TestPartialStraddleScheduledReview:
    def test_closed_monitor_leg_review_uses_remaining_ce_quote(
        self, store: TradingStore, clock: FrozenClock
    ) -> None:
        """Invariant 6: partial straddle review quotes the remaining open leg."""
        put_contract = f.option_contract(
            symbol="NIFTY26OCT22950PE",
            strike=Decimal("22950"),
            option_type=OptionType.PUT,
        )
        call_contract = f.option_contract(
            symbol="NIFTY26OCT22950CE", strike=Decimal("22950")
        )
        entry_legs = (
            f.position_leg_state(leg_id="put", contract=put_contract, side=Side.BUY),
            f.position_leg_state(leg_id="call", contract=call_contract, side=Side.BUY),
        )
        intent = _positional_strangle_intent(entry_legs)
        policy = build_exit_policy(
            f.exit_template(stop_distance_ticks=40),
            trade_id="TRD-REV-017",
            policy_id="EXIT-REV-017",
            entry_price=f.price("88.00"),
            initialized_at=NOW,
            scope=ExitScope.LEG_PRICE,
            quantity_contracts=65,
        )
        open_leg = f.position_leg_state(
            leg_id="call",
            contract=call_contract,
            side=Side.BUY,
            quantity_contracts=65,
            average_entry_price=f.price("88.00"),
        )
        position = f.position_state(
            trade_id="TRD-REV-017",
            state=TradeState.OPEN,
            legs=(open_leg,),
            entry_legs=entry_legs,
            exit_policy=policy,
            protective_order_ids=("PROT-1",),
            opened_at=NOW,
            mode_id=ModeId.M3_TACTICAL_POSITIONAL,
        )
        store.upsert_position_lifecycle(
            PositionLifecycleRecord(
                trade_id=position.trade_id,
                position=position,
                intent=intent,
                risk_decision=f.risk_decision(intent_id=intent.intent_id),
                holding_style=HoldingStyle.POSITIONAL,
                as_of=position.as_of,
            ),
            event_id="PLC-REV-017",
        )
        runner = _discovery_runner(store, clock)
        runner.recover_lifecycle()
        call_symbol = call_contract.symbol
        runner.on_quote_update(
            {call_symbol: _rest_quote("90.00")},
            source=QuoteMonitorSource.REST,
            received_at=clock.now_utc(),
            quote_max_age_ms=DISCOVERY.hard_quote_max_age_ms,
        )
        result = runner.run_review_slot(
            MORNING_SLOT,
            {},
            session_date=SESSION_DATE,
        )
        assert result.decisions
        assert all(
            decision.reason_code is not ReasonCode.UNPROTECTED_POSITION
            for decision in result.decisions
        )


class TestScheduledReviewRestProtection:
    def test_off_chain_leg_uses_rest_in_scheduled_review(
        self, store: TradingStore, clock: FrozenClock
    ) -> None:
        """Invariant 6: scheduled review merges REST protection for off-chain legs."""
        runner = _discovery_runner(store, clock)
        legs = _following_week_legs()
        _open_positional_strangle(runner, store, legs)
        put_symbol = legs[0].contract.symbol
        call_symbol = legs[1].contract.symbol
        cycle_snapshots = {
            put_symbol: _mid_snapshot(legs[0].contract, mid="140.00"),
        }
        runner.on_quote_update(
            {call_symbol: _rest_quote("205.00")},
            source=QuoteMonitorSource.REST,
            received_at=clock.now_utc(),
            quote_max_age_ms=DISCOVERY.hard_quote_max_age_ms,
        )
        result = runner.run_review_slot(
            MORNING_SLOT,
            cycle_snapshots,
            session_date=SESSION_DATE,
        )
        assert result.decisions
        assert all(
            decision.reason_code is not ReasonCode.UNPROTECTED_POSITION
            for decision in result.decisions
        )
        position = runner.trade_manager.get_position("TRD-A26")
        assert position is not None
        assert not position.software_stop_unavailable
        assert not position.protection_degraded
        freeze = store.get_entry_freeze()
        assert freeze is None or freeze.entries_blocked is False


class TestPartialStructureRecovery:
    def test_discovery_partial_exit_does_not_freeze_entries(
        self, store: TradingStore, clock: FrozenClock
    ) -> None:
        """Invariant 15: reconciled remaining legs must not raise PARTIAL_FILL_UNREPAIRED."""
        put_contract = f.option_contract(
            symbol="NIFTY26OCT22950PE",
            strike=Decimal("22950"),
            option_type=OptionType.PUT,
        )
        call_contract = f.option_contract(
            symbol="NIFTY26OCT22950CE", strike=Decimal("22950")
        )
        intent = _positional_strangle_intent(
            (
                f.position_leg_state(
                    leg_id="put", contract=put_contract, side=Side.BUY
                ),
                f.position_leg_state(
                    leg_id="call", contract=call_contract, side=Side.BUY
                ),
            )
        )
        policy = build_discovery_exit_policy(
            intent,
            (
                f.position_leg_state(
                    leg_id="call",
                    contract=call_contract,
                    side=Side.BUY,
                    average_entry_price=f.price("88.00"),
                ),
            ),
            f.exit_template(stop_distance_ticks=40),
            trade_id="TRD-PARTIAL-A26",
            policy_id="EXIT-PARTIAL-A26",
            initialized_at=NOW,
            config=EXITS,
            scope=ExitScope.STRATEGY_PNL,
        )
        entry_legs = (
            f.position_leg_state(
                leg_id="put",
                contract=put_contract,
                side=Side.BUY,
                quantity_contracts=65,
                average_entry_price=f.price("90.00"),
            ),
            f.position_leg_state(
                leg_id="call",
                contract=call_contract,
                side=Side.BUY,
                quantity_contracts=65,
                average_entry_price=f.price("88.00"),
            ),
        )
        position = f.position_state(
            trade_id="TRD-PARTIAL-A26",
            intent_id=intent.intent_id,
            state=TradeState.OPEN,
            legs=(entry_legs[1],),
            entry_legs=entry_legs,
            exit_policy=policy,
            protective_order_ids=("PROT-1",),
            opened_at=NOW,
            mode_id=ModeId.M3_TACTICAL_POSITIONAL,
        )
        record = PositionLifecycleRecord(
            trade_id=position.trade_id,
            position=position,
            intent=intent,
            risk_decision=f.risk_decision(intent_id=intent.intent_id),
            holding_style=HoldingStyle.POSITIONAL,
            as_of=position.as_of,
        )
        store.upsert_position_lifecycle(record, event_id="PLC-PARTIAL-A26")
        broker = PaperBroker.from_fixtures(
            BROKER_FIXTURES, clock=clock, id_factory=SequentialIdFactory(clock.instant)
        )
        payload = broker.dump_state()
        payload["positions"] = [
            f.position_record(
                trade_id=position.trade_id,
                contract=call_contract,
                side=Side.BUY,
                quantity_contracts=65,
            ).model_dump(mode="json")
        ]
        broker.load_state(payload)
        runner = _discovery_runner_with_broker(store, clock, broker)
        recovery = runner.recover_lifecycle()
        assert recovery.entries_blocked is False
        assert not any(
            alert.reason_code is ReasonCode.PARTIAL_FILL_UNREPAIRED
            for alert in recovery.alerts
        )
        freeze = store.get_entry_freeze()
        assert freeze is None or freeze.entries_blocked is False

    def test_entry_legs_and_filled_exit_do_not_freeze_on_restart(
        self, store: TradingStore, clock: FrozenClock
    ) -> None:
        """Invariant 15: closed legs with broker FILLED exits are not partial-fill drift."""
        put_contract = f.option_contract(
            symbol="NIFTY26OCT22950PE",
            strike=Decimal("22950"),
            option_type=OptionType.PUT,
        )
        call_contract = f.option_contract(
            symbol="NIFTY26OCT22950CE", strike=Decimal("22950")
        )
        entry_legs = (
            f.position_leg_state(
                leg_id="put",
                contract=put_contract,
                side=Side.BUY,
                quantity_contracts=65,
                average_entry_price=f.price("90.00"),
            ),
            f.position_leg_state(
                leg_id="call",
                contract=call_contract,
                side=Side.BUY,
                quantity_contracts=65,
                average_entry_price=f.price("88.00"),
            ),
        )
        intent = _positional_strangle_intent(entry_legs)
        policy = build_discovery_exit_policy(
            intent,
            (entry_legs[1],),
            f.exit_template(stop_distance_ticks=40),
            trade_id="TRD-017",
            policy_id="EXIT-017",
            initialized_at=NOW,
            config=EXITS,
            scope=ExitScope.STRATEGY_PNL,
        )
        position = f.position_state(
            trade_id="TRD-017",
            intent_id=intent.intent_id,
            state=TradeState.OPEN,
            legs=(entry_legs[1],),
            entry_legs=entry_legs,
            exit_policy=policy,
            protective_order_ids=("PROT-1",),
            opened_at=NOW,
            mode_id=ModeId.M3_TACTICAL_POSITIONAL,
        )
        record = PositionLifecycleRecord(
            trade_id=position.trade_id,
            position=position,
            intent=intent,
            risk_decision=f.risk_decision(intent_id=intent.intent_id),
            holding_style=HoldingStyle.POSITIONAL,
            as_of=position.as_of,
        )
        store.upsert_position_lifecycle(record, event_id="PLC-017")
        broker = PaperBroker.from_fixtures(
            BROKER_FIXTURES, clock=clock, id_factory=SequentialIdFactory(clock.instant)
        )
        payload = broker.dump_state()
        payload["positions"] = [
            f.position_record(
                trade_id=position.trade_id,
                contract=call_contract,
                side=Side.BUY,
                quantity_contracts=65,
            ).model_dump(mode="json")
        ]
        payload["orders"] = [
            f.order_event(
                identity=f.order_identity(
                    trade_id=position.trade_id, internal_order_id="EXIT-PE"
                ),
                command=f.order_command(
                    contract=put_contract, side=Side.SELL, quantity_contracts=65
                ),
                state=OrderState.FILLED,
                acknowledged_quantity=65,
                filled_quantity=65,
                average_fill_price=f.price("85.00"),
            ).model_dump(mode="json")
        ]
        broker.load_state(payload)
        runner = _discovery_runner_with_broker(store, clock, broker)
        recovery = runner.recover_lifecycle()
        assert recovery.entries_blocked is False
        assert not any(
            alert.reason_code is ReasonCode.PARTIAL_FILL_UNREPAIRED
            for alert in recovery.alerts
        )

    def test_strict_still_flags_partial_structure_mismatch(self) -> None:
        """STRICT recovery keeps PARTIAL_FILL_UNREPAIRED for leg-count drift."""
        long_contract = f.option_contract()
        short_contract = f.option_contract(
            symbol="NIFTY26SEP24200CE", strike=Decimal("24200")
        )
        intent = f.intent(
            legs=(
                IntentLeg(
                    leg_id="long", contract=long_contract, side=Side.BUY, ratio=1
                ),
                IntentLeg(
                    leg_id="short", contract=short_contract, side=Side.SELL, ratio=1
                ),
            )
        )
        policy = build_exit_policy(
            intent.exit_template,
            trade_id="TRD-STRICT-PARTIAL",
            policy_id="EXIT-STRICT-PARTIAL",
            entry_price=f.price("92.00"),
            initialized_at=datetime(2026, 9, 14, 4, 0, tzinfo=UTC),
            scope=ExitScope.STRATEGY_PNL,
            quantity_contracts=75,
        )
        position = f.position_state(
            trade_id="TRD-STRICT-PARTIAL",
            intent_id=intent.intent_id,
            state=TradeState.OPEN,
            legs=(
                f.position_leg_state(
                    leg_id="long",
                    contract=long_contract,
                    side=Side.BUY,
                    quantity_contracts=75,
                    average_entry_price=f.price("92.00"),
                ),
            ),
            exit_policy=policy,
            protective_order_ids=("PROT-1",),
            opened_at=datetime(2026, 9, 14, 4, 0, tzinfo=UTC),
        )
        record = PositionLifecycleRecord(
            trade_id=position.trade_id,
            position=position,
            intent=intent,
            risk_decision=f.risk_decision(intent_id=intent.intent_id),
            holding_style=HoldingStyle.POSITIONAL,
            as_of=position.as_of,
        )
        broker_legs = (
            f.position_record(
                trade_id=position.trade_id,
                contract=long_contract,
                side=Side.BUY,
                quantity_contracts=75,
            ),
        )
        alerts = _lifecycle_issues(record, broker_legs)
        assert any(
            alert.reason_code is ReasonCode.PARTIAL_FILL_UNREPAIRED for alert in alerts
        )
