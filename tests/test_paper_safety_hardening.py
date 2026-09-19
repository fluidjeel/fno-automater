"""P0 PAPER safety hardening: snapshot bundle, freeze, missing monitor, stale.

Invariant 6: stale/invalid quotes block new exposure and degrade open protection.
Invariant 8: existing positions keep deterministic protection after restart.
Invariant 9: restart restores lifecycle and the entry freeze latch.
Invariant 16: a missing monitor is UNPROTECTED_POSITION, never a silent HOLD.
Invariant 18: multi-leg decisions keep per-leg quote snapshot provenance.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

import tests.factories as f
from tests.test_paper_lifecycle import _open_long, _option_snapshot, _restart
from tests.test_paper_review import _engine_inputs
from tests.test_paper_runner import ROOT, _request
from tests.test_risk_gateway import instrument_spec
from trading.broker.paper import PaperBroker
from trading.config import load_config, load_risk_policy
from trading.domain.clock import FrozenClock
from trading.domain.contracts import (
    DerivativesContext,
    EntryFreezeRecord,
    FeatureSnapshot,
    Greeks,
    IntentLeg,
    RiskDecision,
    TradeIntent,
)
from trading.domain.enums import (
    DataQuality,
    HoldingStyle,
    OptionType,
    OrderState,
    ReasonCode,
    ReviewAction,
    ReviewSlotId,
    RiskAction,
    Side,
    TradeState,
)
from trading.domain.ids import SequentialIdFactory
from trading.risk import RiskGateway, RiskGatewayRequest
from trading.risk.reservation import CapitalReservationService
from trading.storage.trading_store import TradingEventType, TradingStore
from trading.trade.review import ReviewEngine

NOW = f.NOW
ACCOUNT_CONFIG = load_config(ROOT / "config" / "base.yaml")
RISK_POLICY = load_risk_policy(ROOT / "config" / "risk.yaml")
BROKER_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "broker"


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(NOW + timedelta(seconds=60))


@pytest.fixture
def store(tmp_path: Path, clock: FrozenClock) -> Iterator[TradingStore]:
    trading_store = TradingStore.open(tmp_path / "paper.sqlite", clock=clock)
    yield trading_store
    trading_store.close()


def _long_contract() -> object:
    return f.option_contract(symbol="NIFTY26SEP24000CE", strike=Decimal("24000"))


def _short_contract() -> object:
    return f.option_contract(symbol="NIFTY26SEP24200CE", strike=Decimal("24200"))


def _option_quote(
    contract: object,
    *,
    snapshot_id: str,
    bid: str,
    ask: str,
    **overrides: object,
) -> FeatureSnapshot:
    payload: dict[str, object] = {
        "snapshot_id": snapshot_id,
        "contract": contract,
        "market": f.quote(
            bid=f.price(bid), ask=f.price(ask), bid_size=300, ask_size=300
        ),
        "derivatives": DerivativesContext(
            days_to_expiry=10,
            open_interest=5000,
            option_type=OptionType.CALL,
            underlying_price=f.price("24000"),
            greeks=Greeks(
                model="bs",
                calculation_version="1",
                converged=True,
                delta=Decimal("0.55"),
            ),
        ),
    }
    payload.update(overrides)
    return f.snapshot(**payload)


def _debit_intent(snapshot_id: str) -> TradeIntent:
    return f.intent(
        snapshot_id=snapshot_id,
        strategy_id="debit_spread",
        requested_risk=f.money("4000"),
        estimated_max_loss=f.money("6000"),
        legs=(
            IntentLeg(
                leg_id="leg-long",
                contract=_long_contract(),  # type: ignore[arg-type]
                side=Side.BUY,
                ratio=1,
            ),
            IntentLeg(
                leg_id="leg-short",
                contract=_short_contract(),  # type: ignore[arg-type]
                side=Side.SELL,
                ratio=1,
            ),
        ),
    )


def _evaluate_debit(
    store: TradingStore,
    clock: FrozenClock,
    *,
    intent: TradeIntent,
    feature: FeatureSnapshot,
    leg_snapshots: dict[str, FeatureSnapshot],
) -> RiskDecision:
    return _gateway(store, clock).evaluate(
        RiskGatewayRequest(
            intent=intent,
            feature_snapshot=feature,
            portfolio_snapshot=f.portfolio_snapshot(),
            instrument=instrument_spec(),
            leg_snapshots=leg_snapshots,
            event_risk_state=f.event_risk_state(),
        )
    )


def _gateway(store: TradingStore, clock: FrozenClock) -> RiskGateway:
    ids = SequentialIdFactory(clock.instant)
    broker = PaperBroker.from_fixtures(BROKER_FIXTURES, clock=clock, id_factory=ids)
    return RiskGateway(
        account_config=ACCOUNT_CONFIG,
        risk_policy=RISK_POLICY,
        reservation_service=CapitalReservationService(
            store, clock=clock, id_factory=ids
        ),
        margin_preview=broker,
        clock=clock,
        id_factory=ids,
    )


def _freeze_events(store: TradingStore) -> list[object]:
    return [
        stored
        for stored in store.read_events()
        if stored.event_type is TradingEventType.ENTRY_FREEZE
    ]


class TestDebitSpreadSnapshotBundle:
    def test_valid_distinct_leg_quotes_are_approved(
        self, tmp_path: Path, clock: FrozenClock
    ) -> None:
        """Invariant 18: debit spreads pass with distinct per-leg quote snapshot IDs."""
        store = TradingStore.open(tmp_path / "gw.sqlite", clock=clock)
        clock.set(NOW)
        long_snap = _option_quote(
            _long_contract(), snapshot_id="SNAP-LONG", bid="91.95", ask="92.00"
        )
        short_snap = _option_quote(
            _short_contract(), snapshot_id="SNAP-SHORT", bid="44.95", ask="45.00"
        )
        intent = _debit_intent("SNAP-CYCLE")
        feature = long_snap.model_copy(update={"snapshot_id": "SNAP-CYCLE"})
        decision = _evaluate_debit(
            store,
            clock,
            intent=intent,
            feature=feature,
            leg_snapshots={"leg-long": long_snap, "leg-short": short_snap},
        )
        assert decision.action in {RiskAction.APPROVE, RiskAction.RESIZE}
        ids = {item.snapshot_id for item in decision.leg_quotes}
        assert ids == {"SNAP-LONG", "SNAP-SHORT"}
        assert "SNAP-CYCLE" not in ids
        assert decision.decision_snapshot_id == "SNAP-CYCLE"
        store.close()

    def test_mismatched_leg_snapshot_is_rejected(
        self, tmp_path: Path, clock: FrozenClock
    ) -> None:
        """A quote that does not identify the intent leg is SNAPSHOT_MISMATCH."""
        store = TradingStore.open(tmp_path / "gw.sqlite", clock=clock)
        clock.set(NOW)
        long_snap = _option_quote(
            _long_contract(), snapshot_id="SNAP-LONG", bid="91.95", ask="92.00"
        )
        wrong = _option_quote(
            _long_contract(), snapshot_id="SNAP-WRONG", bid="44.95", ask="45.00"
        )
        intent = _debit_intent("SNAP-CYCLE")
        feature = long_snap.model_copy(update={"snapshot_id": "SNAP-CYCLE"})
        decision = _evaluate_debit(
            store,
            clock,
            intent=intent,
            feature=feature,
            leg_snapshots={"leg-long": long_snap, "leg-short": wrong},
        )
        assert decision.action is RiskAction.REJECT
        assert ReasonCode.SNAPSHOT_MISMATCH in decision.reason_codes
        store.close()

    def test_skewed_leg_timestamps_are_rejected(
        self, tmp_path: Path, clock: FrozenClock
    ) -> None:
        """Legs from clocks more than max_leg_quote_skew_ms apart fail closed."""
        store = TradingStore.open(tmp_path / "gw.sqlite", clock=clock)
        clock.set(NOW)
        long_snap = _option_quote(
            _long_contract(), snapshot_id="SNAP-LONG", bid="91.95", ask="92.00"
        )
        early = NOW - timedelta(seconds=5)
        short_snap = _option_quote(
            _short_contract(),
            snapshot_id="SNAP-SHORT",
            bid="44.95",
            ask="45.00",
            times=f.snapshot_times(
                event_time=early,
                source_time=early,
                receive_time=early + timedelta(milliseconds=50),
                calculation_time=early + timedelta(milliseconds=120),
            ),
        )
        intent = _debit_intent("SNAP-CYCLE")
        feature = long_snap.model_copy(update={"snapshot_id": "SNAP-CYCLE"})
        decision = _evaluate_debit(
            store,
            clock,
            intent=intent,
            feature=feature,
            leg_snapshots={"leg-long": long_snap, "leg-short": short_snap},
        )
        assert decision.action is RiskAction.REJECT
        assert ReasonCode.SNAPSHOT_MISMATCH in decision.reason_codes
        store.close()

    def test_future_leg_quote_is_rejected(
        self, tmp_path: Path, clock: FrozenClock
    ) -> None:
        """A quote whose event_time is after the decision clock is lookahead."""
        store = TradingStore.open(tmp_path / "gw.sqlite", clock=clock)
        clock.set(NOW)
        future = NOW + timedelta(seconds=2)
        long_snap = _option_quote(
            _long_contract(),
            snapshot_id="SNAP-LONG",
            bid="91.95",
            ask="92.00",
            times=f.snapshot_times(
                event_time=future,
                source_time=future,
                receive_time=future + timedelta(milliseconds=50),
                calculation_time=future + timedelta(milliseconds=120),
            ),
        )
        short_snap = _option_quote(
            _short_contract(), snapshot_id="SNAP-SHORT", bid="44.95", ask="45.00"
        )
        intent = _debit_intent("SNAP-CYCLE")
        feature = long_snap.model_copy(update={"snapshot_id": "SNAP-CYCLE"})
        decision = _evaluate_debit(
            store,
            clock,
            intent=intent,
            feature=feature,
            leg_snapshots={"leg-long": long_snap, "leg-short": short_snap},
        )
        assert decision.action is RiskAction.REJECT
        assert ReasonCode.SNAPSHOT_MISMATCH in decision.reason_codes
        store.close()


class TestPersistentEntryFreeze:
    def test_restart_restores_entries_blocked(
        self, store: TradingStore, clock: FrozenClock
    ) -> None:
        """Invariant 9: the persisted freeze latch is restored after restart."""
        first = _open_long(store, clock)
        first._ensure_entries_blocked(
            ReasonCode.UNPROTECTED_POSITION,
            "seeded freeze for restart; PAPER has no broker-resident stop",
        )
        opened = first.trade_manager.list_positions()[0]
        seeded = opened.model_copy(
            update={
                "protection_degraded": True,
                "software_stop_unavailable": True,
                "unprotected_reason": "seeded freeze for restart",
                "protection_degraded_since": clock.now_utc(),
            }
        )
        first.trade_manager.restore_position(seeded)
        first._write_lifecycle(opened.trade_id)
        freeze_count = len(_freeze_events(store))
        assert freeze_count >= 1

        second = _restart(store, clock, first.broker)
        recovery = second.recover_lifecycle()
        assert recovery.entries_blocked is True
        persisted = store.get_entry_freeze()
        assert persisted is not None
        assert persisted.entries_blocked is True
        blocked = second.run_cycle((_request(),))
        assert blocked.outcomes[0].order_events == ()

    def test_restart_with_exit_pending_keeps_freeze(
        self, store: TradingStore, clock: FrozenClock
    ) -> None:
        """EXIT_PENDING after restart must keep entries_blocked until reconcile."""
        first = _open_long(store, clock)
        opened = first.trade_manager.list_positions()[0]
        pending = opened.model_copy(update={"state": TradeState.EXIT_PENDING})
        record = store.get_position_lifecycle(opened.trade_id)
        assert record is not None
        unknown = f.order_event(
            event_id="EVT-EXIT-UNKNOWN",
            state=OrderState.UNKNOWN,
            reason_code=ReasonCode.ORDER_TIMEOUT,
            filled_quantity=0,
            acknowledged_quantity=0,
            identity=f.order_identity(
                internal_order_id="ORD-EXIT-UNKNOWN",
                client_order_id="ORD-EXIT-UNKNOWN",
                idempotency_key="b" * 32,
                trade_id=opened.trade_id,
                intent_id=opened.intent_id,
                broker_order_id="UNRESOLVED-ORD-EXIT-UNKNOWN",
            ),
            command=f.order_command(
                side=Side.SELL,
                contract=opened.legs[0].contract,
                quantity_contracts=opened.legs[0].quantity_contracts,
            ),
        )
        store.append(TradingEventType.ORDER_EVENT, unknown, event_id=unknown.event_id)
        store.upsert_position_lifecycle(
            record.model_copy(
                update={
                    "position": pending,
                    "exit_order_ids": ("ORD-EXIT-UNKNOWN",),
                    "as_of": pending.as_of,
                }
            ),
            event_id="PLC-UNKNOWN-FREEZE",
        )
        second = _restart(store, clock, first.broker)
        recovery = second.recover_lifecycle()
        assert recovery.entries_blocked is True
        assert any(
            alert.reason_code is ReasonCode.UNKNOWN_ORDER_STATUS
            for alert in recovery.alerts
        )
        persisted = store.get_entry_freeze()
        assert persisted is not None
        assert persisted.entries_blocked is True
        assert persisted.reason_code is ReasonCode.UNKNOWN_ORDER_STATUS

    def test_duplicate_freeze_and_recovery_events_are_idempotent(
        self, store: TradingStore, clock: FrozenClock
    ) -> None:
        """Restart recovery must not append duplicate freeze audit events."""
        first = _open_long(store, clock)
        first._ensure_entries_blocked(
            ReasonCode.UNKNOWN_ORDER_STATUS,
            "exit order status is unknown; replacement is blocked until reconciliation",
        )
        first._ensure_entries_blocked(
            ReasonCode.UNKNOWN_ORDER_STATUS,
            "exit order status is unknown; replacement is blocked until reconciliation",
        )
        freeze_count = len(_freeze_events(store))
        assert freeze_count == 1
        second = _restart(store, clock, first.broker)
        second.recover_lifecycle()
        second.recover_lifecycle()
        assert len(_freeze_events(store)) == freeze_count


class TestMissingMonitor:
    def test_missing_monitor_before_entry_is_rejected(
        self, tmp_path: Path, clock: FrozenClock
    ) -> None:
        """A new debit-spread entry with a missing leg quote is rejected."""
        store = TradingStore.open(tmp_path / "gw.sqlite", clock=clock)
        clock.set(NOW)
        long_snap = _option_quote(
            _long_contract(), snapshot_id="SNAP-LONG", bid="91.95", ask="92.00"
        )
        intent = _debit_intent("SNAP-CYCLE")
        feature = long_snap.model_copy(update={"snapshot_id": "SNAP-CYCLE"})
        decision = _evaluate_debit(
            store,
            clock,
            intent=intent,
            feature=feature,
            leg_snapshots={"leg-long": long_snap},
        )
        assert decision.action is RiskAction.REJECT
        assert ReasonCode.SNAPSHOT_MISMATCH in decision.reason_codes
        store.close()

    def test_missing_monitor_after_entry_does_not_hold(
        self, store: TradingStore, clock: FrozenClock
    ) -> None:
        """Invariant 16: an open with no monitor is UNPROTECTED, never silent HOLD."""
        runner = _open_long(store, clock)
        opened = runner.trade_manager.list_positions()[0]
        events = runner.manage_exits({})
        assert events == ()
        still = runner.trade_manager.get_position(opened.trade_id)
        assert still is not None
        assert still.state is TradeState.OPEN
        assert still.software_stop_unavailable is True
        assert still.unprotected_reason is not None
        assert "broker-resident" in still.unprotected_reason
        freeze = store.get_entry_freeze()
        assert freeze is not None
        assert freeze.entries_blocked is True
        assert freeze.reason_code is ReasonCode.UNPROTECTED_POSITION
        blocked = runner.run_cycle((_request(),))
        assert blocked.outcomes[0].order_events == ()


class TestStaleProtection:
    def test_stale_quote_while_open_freezes_and_records_gap(
        self, store: TradingStore, clock: FrozenClock
    ) -> None:
        """Stale quotes degrade protection, freeze entries, and keep the position."""
        runner = _open_long(store, clock)
        opened = runner.trade_manager.list_positions()[0]
        symbol = opened.legs[0].contract.symbol
        stale = _option_snapshot(
            opened.legs[0].contract,
            market=f.quote(bid=f.price("1.00"), ask=f.price("1.05")),
            quality=f.quality(
                state=DataQuality.STALE, reason_codes=(ReasonCode.DATA_STALE,)
            ),
        )
        events = runner.manage_exits({symbol: stale})
        assert events == ()
        still = runner.trade_manager.get_position(opened.trade_id)
        assert still is not None
        assert still.state is TradeState.OPEN
        assert still.protection_degraded is True
        assert still.software_stop_unavailable is True
        assert still.unprotected_reason is not None
        assert "broker-resident" in still.unprotected_reason
        freeze = store.get_entry_freeze()
        assert freeze is not None
        assert freeze.entries_blocked is True
        assert freeze.reason_code is ReasonCode.PROTECTION_DEGRADED
        assert freeze.detail is not None
        assert "broker-resident" in freeze.detail

    def test_fresh_quote_clears_degraded_after_reconcile(
        self, store: TradingStore, clock: FrozenClock
    ) -> None:
        """Fresh quotes clear PROTECTION_DEGRADED; freeze lifts after reconcile."""
        runner = _open_long(store, clock)
        opened = runner.trade_manager.list_positions()[0]
        symbol = opened.legs[0].contract.symbol
        stale = _option_snapshot(
            opened.legs[0].contract,
            quality=f.quality(
                state=DataQuality.STALE, reason_codes=(ReasonCode.DATA_STALE,)
            ),
        )
        runner.manage_exits({symbol: stale})
        fresh = _option_snapshot(
            opened.legs[0].contract,
            market=f.quote(bid=f.price("91.95"), ask=f.price("92.00")),
        )
        runner.manage_exits({symbol: fresh})
        cleared = runner.trade_manager.get_position(opened.trade_id)
        assert cleared is not None
        assert cleared.protection_degraded is False
        assert cleared.software_stop_unavailable is False
        result = runner.run_cycle(())
        freeze = store.get_entry_freeze()
        assert freeze is None or freeze.entries_blocked is False
        assert result.entries_blocked is False


class TestExitTemplateFixtures:
    def test_disabled_template_does_not_tighten_or_partial(self) -> None:
        """Production positional template has no BE/trail, so review HOLDs."""
        position, intent, feature = _engine_inputs(bid="110.00")
        evaluation = ReviewEngine().evaluate(
            position,
            intent,
            feature,
            now=NOW,
            slot_id=ReviewSlotId.NSE_MORNING,
            session_date=NOW.date(),
            holding_style=HoldingStyle.POSITIONAL,
        )
        assert evaluation.action is ReviewAction.HOLD
        assert evaluation.exit_quantity_contracts is None
        assert evaluation.updated_policy is None

    def test_enabled_breakeven_template_tightens_stop(self) -> None:
        """Enabled BE/trail on a winning quote tightens the frozen software stop."""
        position, intent, feature = _engine_inputs(
            bid="110.00",
            trail=True,
            entry="100.00",
            stop="90.00",
        )
        evaluation = ReviewEngine().evaluate(
            position,
            intent,
            feature,
            now=NOW,
            slot_id=ReviewSlotId.NSE_MORNING,
            session_date=NOW.date(),
            holding_style=HoldingStyle.POSITIONAL,
        )
        assert evaluation.action is ReviewAction.TIGHTEN_STOP
        assert evaluation.updated_policy is not None
        assert evaluation.updated_policy.stop_price is not None
        assert position.exit_policy.stop_price is not None
        assert (
            evaluation.updated_policy.stop_price.value
            > position.exit_policy.stop_price.value
        )

    def test_enabled_partial_exit_when_breakeven_active(self) -> None:
        """Enabled BE that is already active scales out instead of HOLD."""
        position, intent, feature = _engine_inputs(
            bid="110.00",
            trail=True,
            breakeven_active=True,
            qty=75,
        )
        evaluation = ReviewEngine().evaluate(
            position,
            intent,
            feature,
            now=NOW,
            slot_id=ReviewSlotId.NSE_MORNING,
            session_date=NOW.date(),
            holding_style=HoldingStyle.POSITIONAL,
        )
        assert evaluation.action is ReviewAction.PARTIAL_EXIT
        assert evaluation.exit_quantity_contracts is not None
        assert 0 < evaluation.exit_quantity_contracts < 75


def test_entry_freeze_upsert_is_idempotent(tmp_path: Path, clock: FrozenClock) -> None:
    """Unchanged freeze rows do not append a second audit event."""
    store = TradingStore.open(tmp_path / "frz.sqlite", clock=clock)
    record = EntryFreezeRecord(
        entries_blocked=True,
        reason_code=ReasonCode.PROTECTION_DEGRADED,
        detail="required quotes are stale; PAPER has no broker-resident stop",
        updated_at=clock.now_utc(),
    )
    assert store.upsert_entry_freeze(record, event_id="FRZ-1") is True
    assert store.upsert_entry_freeze(record, event_id="FRZ-2") is False
    assert len(_freeze_events(store)) == 1
    store.close()
