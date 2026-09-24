"""PAPER twice-daily positional review (PAPER-007).

Invariant 8: continuous software stops still fire between scheduled slots.
Invariant 11: duplicate slot and restart do not submit a second exit.
Invariant 17: review tightening never widens the frozen stop.
Invariant 2: HEDGE/ROLL are proposals; they never auto-submit.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import tests.factories as f
from tests.test_paper_lifecycle import (
    _open_long,
    _option_snapshot,
    _restart,
    _Sink,
)
from tests.test_paper_runner import ROOT, _request
from trading.domain.clock import FrozenClock
from trading.domain.contracts import (
    DerivativesContext,
    FeatureSnapshot,
    PositionLifecycleRecord,
    PositionReviewRecord,
    TradeIntent,
)
from trading.domain.contracts.carry import PositionCarryRecord
from trading.domain.contracts.position import PositionState
from trading.domain.enums import (
    CarryGateAction,
    HoldingStyle,
    ModeId,
    OptionType,
    ReasonCode,
    ReviewAction,
    ReviewSlotId,
    Side,
    TradeState,
)
from trading.domain.primitives import Currency, Money
from trading.runtime.notify import format_review_decision
from trading.runtime.paper_runner import PaperRunner
from trading.runtime.paper_session import PaperSession, load_paper_session_config
from trading.runtime.review_schedule import ReviewSlot, due_review_slots, parse_hhmm
from trading.storage.trading_store import TradingStore
from trading.trade import ReviewEngine, assert_stop_not_wider, build_exit_policy

NOW = datetime(2026, 9, 14, 4, 0, tzinfo=UTC)
SLOT_1030 = datetime(2026, 9, 14, 5, 0, tzinfo=UTC)
SLOT_1100 = datetime(2026, 9, 14, 5, 30, tzinfo=UTC)
SLOT_1430 = datetime(2026, 9, 14, 9, 0, tzinfo=UTC)
IST = ZoneInfo("Asia/Kolkata")
SESSION_DATE = date(2026, 9, 14)


def _engine_inputs(
    *,
    qty: int = 75,
    stop: str = "90.00",
    entry: str = "100.00",
    bid: str = "100.00",
    dte: int = 10,
    expiry_days: int | None = None,
    trail: bool = False,
    breakeven_active: bool = False,
    trailing_active: bool = False,
) -> tuple[PositionState, TradeIntent, FeatureSnapshot]:
    template_kwargs: dict[str, object] = {
        "stop_distance_ticks": 200,
        "target_distance_ticks": 400,
        "exit_before_expiry_days": expiry_days,
    }
    if trail:
        template_kwargs["trailing_activation_ticks"] = 50
        template_kwargs["trailing_distance_ticks"] = 80
    intent = f.intent(exit_template=f.exit_template(**template_kwargs))
    policy = build_exit_policy(
        intent.exit_template,
        trade_id="TRD-1",
        policy_id="EXIT-POL-1",
        entry_price=f.price(entry),
        initialized_at=NOW,
    )
    policy = policy.model_copy(
        update={
            "stop_price": f.price(stop),
            "breakeven_active": breakeven_active,
            "trailing_active": trailing_active,
        }
    )
    position = f.position_state(
        trade_id="TRD-1",
        intent_id=intent.intent_id,
        exit_policy=policy,
        legs=(
            f.position_leg_state(
                quantity_contracts=qty,
                average_entry_price=f.price(entry),
                current_stop_price=f.price(stop),
            ),
        ),
    )
    feature = _option_snapshot(
        position.legs[0].contract,
        market=f.quote(bid=f.price(bid), ask=f.price(bid)),
        derivatives=DerivativesContext(
            days_to_expiry=dte,
            open_interest=5000,
            option_type=OptionType.CALL,
            underlying_price=f.price("24000"),
        ),
    )
    return position, intent, feature


def _nse_slots() -> tuple[ReviewSlot, ...]:
    return (
        ReviewSlot(ReviewSlotId.NSE_MORNING, parse_hhmm("10:30")),
        ReviewSlot(ReviewSlotId.NSE_AFTERNOON, parse_hhmm("14:30")),
    )


def _stamp_carry_approved(
    runner: PaperRunner,
    trade_id: str,
    *,
    session_date: date = SESSION_DATE,
) -> PositionLifecycleRecord:
    record = PositionCarryRecord(
        carry_id="CRR-TEST",
        trade_id=trade_id,
        session_date=session_date,
        action=CarryGateAction.CARRY_APPROVED,
        mode_id=ModeId.M2_DIRECTIONAL,
        reason_code=ReasonCode.OK,
        detail="test carry approval for scheduled review",
        exit_initiated=False,
        as_of=SLOT_1030,
    )
    runner._write_lifecycle(trade_id, extra_carry=(record,))
    lifecycle = runner._services.store.get_position_lifecycle(trade_id)
    assert lifecycle is not None
    return lifecycle


def _open_reviewable_long(store: TradingStore, clock: FrozenClock) -> PaperRunner:
    runner = _open_long(store, clock)
    position = runner.trade_manager.list_positions()[0]
    _stamp_carry_approved(runner, position.trade_id)
    return runner


def _session(
    runner: PaperRunner,
    clock: FrozenClock,
    tmp_path: Path,
    sink: _Sink,
) -> PaperSession:
    session_cfg = load_paper_session_config(ROOT / "config" / "paper_session.yaml")
    request = _request()
    snapshots = {
        request.candidates[0].contract.symbol: request.candidates[0],
        request.underlying.contract.symbol: request.underlying,
    }

    def builder(
        _now: datetime,
    ) -> tuple[tuple[object, ...], dict[str, FeatureSnapshot]]:
        return ((), snapshots)

    return PaperSession(
        runner=runner,
        clock=clock,
        session_config=session_cfg,
        session_hours=(time(9, 15), time(15, 30)),
        timezone=IST,
        notifier=sink,
        request_builder=builder,  # type: ignore[arg-type]
        observation_start=NOW,
        capital_limit=Money.of("700000", Currency.INR),
        risk_policy_version="4",
        fill_model_version="conservative-v1",
        code_version="1",
        charges_verified=False,
        cohort_dir=tmp_path / "cohorts",
    )


class TestDueSlots:
    def test_both_nse_slots_become_due_in_order(self) -> None:
        morning = SLOT_1030.astimezone(IST)
        afternoon = SLOT_1430.astimezone(IST)
        slots = _nse_slots()
        first = due_review_slots(
            now_local=morning,
            session_open=time(9, 15),
            eod=time(15, 40),
            slots=slots,
            recorded=frozenset(),
        )
        assert [item.slot_id for item in first] == [ReviewSlotId.NSE_MORNING]
        recovery = due_review_slots(
            now_local=afternoon,
            session_open=time(9, 15),
            eod=time(15, 40),
            slots=slots,
            recorded=frozenset(),
        )
        assert len(recovery) == 1
        assert recovery[0].is_recovery is True
        assert recovery[0].slot_id is ReviewSlotId.NSE_AFTERNOON
        assert recovery[0].missed_slot_ids == (
            ReviewSlotId.NSE_MORNING,
            ReviewSlotId.NSE_AFTERNOON,
        )

    def test_missed_morning_is_due_after_restart_before_eod(self) -> None:
        """If 10:30 was missed while down, run it once after restart in session."""
        now_local = SLOT_1100.astimezone(IST)
        due = due_review_slots(
            now_local=now_local,
            session_open=time(9, 15),
            eod=time(15, 40),
            slots=_nse_slots(),
            recorded=frozenset(),
        )
        assert [item.slot_id for item in due] == [ReviewSlotId.NSE_MORNING]

    def test_recorded_slot_is_not_due_again(self) -> None:
        now_local = SLOT_1100.astimezone(IST)
        due = due_review_slots(
            now_local=now_local,
            session_open=time(9, 15),
            eod=time(15, 40),
            slots=_nse_slots(),
            recorded=frozenset({(SESSION_DATE, ReviewSlotId.NSE_MORNING)}),
        )
        assert due == ()

    def test_empty_mcx_slots_never_fire(self) -> None:
        now_local = SLOT_1430.astimezone(IST)
        due = due_review_slots(
            now_local=now_local,
            session_open=time(9, 15),
            eod=time(15, 40),
            slots=_nse_slots(),
            recorded=frozenset(),
        )
        assert all(item.slot_id.venue.value != "MCX" for item in due)


class TestReviewEngine:
    def test_hold_when_frozen_policy_is_unchanged(self) -> None:
        position, intent, feature = _engine_inputs()
        result = ReviewEngine().evaluate(
            position,
            intent,
            feature,
            now=SLOT_1030,
            slot_id=ReviewSlotId.NSE_MORNING,
            session_date=SESSION_DATE,
            holding_style=HoldingStyle.POSITIONAL,
        )
        assert result.action is ReviewAction.HOLD
        assert result.reason_code is ReasonCode.OK
        assert "positional review holds" in result.detail

    def test_tighten_stop_is_monotonic(self) -> None:
        """Invariant 17: trail may only raise a long stop."""
        position, intent, feature = _engine_inputs(
            trail=True, bid="103.00", stop="90.00", entry="100.00"
        )
        initial = position.exit_policy.stop_price
        assert initial is not None
        result = ReviewEngine().evaluate(
            position,
            intent,
            feature,
            now=SLOT_1030,
            slot_id=ReviewSlotId.NSE_MORNING,
            session_date=SESSION_DATE,
            holding_style=HoldingStyle.POSITIONAL,
        )
        assert result.action is ReviewAction.TIGHTEN_STOP
        assert result.updated_policy is not None
        assert result.updated_policy.stop_price is not None
        assert result.updated_policy.stop_price.value > initial.value
        assert_stop_not_wider(position.exit_policy, result.updated_policy, Side.BUY)

    def test_widen_is_rejected(self) -> None:
        """Invariant 17: a wider candidate stop is not applied."""
        position, _intent, _feature = _engine_inputs(stop="95.00", entry="100.00")
        wider = position.exit_policy.model_copy(
            update={
                "stop_price": f.price("80.00"),
                "current_stop_distance_ticks": 400,
            }
        )
        with pytest.raises(ValueError, match="wider"):
            assert_stop_not_wider(position.exit_policy, wider, Side.BUY)

    def test_partial_exit_when_trail_active_and_qty_allows(self) -> None:
        position, intent, feature = _engine_inputs(
            qty=150, trailing_active=True, bid="101.00"
        )
        result = ReviewEngine().evaluate(
            position,
            intent,
            feature,
            now=SLOT_1030,
            slot_id=ReviewSlotId.NSE_MORNING,
            session_date=SESSION_DATE,
            holding_style=HoldingStyle.POSITIONAL,
        )
        assert result.action is ReviewAction.PARTIAL_EXIT
        assert result.exit_quantity_contracts == 75
        assert result.should_submit_exit

    def test_full_exit_honors_frozen_expiry_flatten(self) -> None:
        position, intent, feature = _engine_inputs(dte=1, expiry_days=1, bid="100.00")
        result = ReviewEngine().evaluate(
            position,
            intent,
            feature,
            now=SLOT_1030,
            slot_id=ReviewSlotId.NSE_MORNING,
            session_date=SESSION_DATE,
            holding_style=HoldingStyle.POSITIONAL,
        )
        assert result.action is ReviewAction.FULL_EXIT
        assert result.reason_code is ReasonCode.CONTRACT_EXPIRED

    def test_hedge_and_roll_are_proposal_only(self) -> None:
        """HEDGE/ROLL require Layer 2; auto-submit is blocked."""
        hedge_pos, hedge_intent, hedge_feat = _engine_inputs(
            stop="90.00", entry="100.00", bid="94.00"
        )
        hedge = ReviewEngine().evaluate(
            hedge_pos,
            hedge_intent,
            hedge_feat,
            now=SLOT_1030,
            slot_id=ReviewSlotId.NSE_MORNING,
            session_date=SESSION_DATE,
            holding_style=HoldingStyle.POSITIONAL,
        )
        assert hedge.action is ReviewAction.PROPOSE_HEDGE
        assert hedge.reason_code is ReasonCode.REVIEW_PROPOSAL_REQUIRES_L2
        assert hedge.should_submit_exit is False

        roll_pos, roll_intent, roll_feat = _engine_inputs(
            dte=1, expiry_days=None, bid="100.00"
        )
        roll = ReviewEngine().evaluate(
            roll_pos,
            roll_intent,
            roll_feat,
            now=SLOT_1030,
            slot_id=ReviewSlotId.NSE_AFTERNOON,
            session_date=SESSION_DATE,
            holding_style=HoldingStyle.POSITIONAL,
        )
        assert roll.action is ReviewAction.PROPOSE_ROLL
        assert roll.reason_code is ReasonCode.REVIEW_PROPOSAL_REQUIRES_L2
        assert roll.should_submit_exit is False

    def test_duplicate_slot_on_the_trade_is_a_no_op(self) -> None:
        position, intent, feature = _engine_inputs()
        prior = (
            PositionReviewRecord(
                review_id="REV-1",
                trade_id="TRD-1",
                slot_id=ReviewSlotId.NSE_MORNING,
                session_date=SESSION_DATE,
                action=ReviewAction.HOLD,
                reason_code=ReasonCode.OK,
                detail="already reviewed",
                submitted=False,
                frozen_policy_id=position.exit_policy.policy_id,
                as_of=SLOT_1030,
            ),
        )
        result = ReviewEngine().evaluate(
            position,
            intent,
            feature,
            now=SLOT_1030,
            slot_id=ReviewSlotId.NSE_MORNING,
            session_date=SESSION_DATE,
            holding_style=HoldingStyle.POSITIONAL,
            prior_reviews=prior,
        )
        assert result.reason_code is ReasonCode.REVIEW_DUPLICATE_SLOT
        assert result.action is ReviewAction.HOLD


class TestPaperSessionReviews:
    def test_both_slots_fire_and_duplicate_tick_is_safe(
        self, store: TradingStore, clock: FrozenClock, tmp_path: Path
    ) -> None:
        runner = _open_reviewable_long(store, clock)
        sink = _Sink()
        session = _session(runner, clock, tmp_path, sink)
        clock.set(SLOT_1030)
        session.tick()
        clock.set(SLOT_1430)
        session.tick()
        session.tick()
        recorded = store.list_review_slot_runs(SESSION_DATE)
        slots = {item[0] for item in recorded}
        assert ReviewSlotId.NSE_MORNING in slots
        assert ReviewSlotId.NSE_AFTERNOON in slots
        opened = next(item for item in store.list_position_lifecycle() if item.reviews)
        morning_reviews = [
            item for item in opened.reviews if item.slot_id is ReviewSlotId.NSE_MORNING
        ]
        assert len(morning_reviews) == 1
        assert all("promote" not in msg.lower() for msg in sink.messages)
        assert any("PAPER review" in msg for msg in sink.messages)
        assert any("not broker-resident" in msg for msg in sink.messages)

    def test_missed_morning_runs_once_after_restart(
        self, store: TradingStore, clock: FrozenClock, tmp_path: Path
    ) -> None:
        first = _open_reviewable_long(store, clock)
        first.flush_lifecycle()
        clock.set(SLOT_1100)
        second = _restart(store, clock, first.broker)
        sink = _Sink()
        session = _session(second, clock, tmp_path, sink)
        session.run(once=True)
        assert store.has_review_slot_run(ReviewSlotId.NSE_MORNING, SESSION_DATE)
        assert not store.has_review_slot_run(ReviewSlotId.NSE_AFTERNOON, SESSION_DATE)
        session.tick()
        morning = [
            item
            for item in store.list_review_slot_runs(SESSION_DATE)
            if item[0] is ReviewSlotId.NSE_MORNING
        ]
        assert len(morning) == 1

    def test_continuous_stop_still_fires_between_reviews(
        self, store: TradingStore, clock: FrozenClock, tmp_path: Path
    ) -> None:
        """Invariant 8: stops never wait for the next scheduled review."""
        runner = _open_reviewable_long(store, clock)
        sink = _Sink()
        opened = runner.trade_manager.list_positions()[0]
        symbol = opened.legs[0].contract.symbol
        session = _session(runner, clock, tmp_path, sink)
        clock.set(SLOT_1030)
        session.tick()
        still = runner.trade_manager.get_position(opened.trade_id)
        assert still is not None
        assert still.state is TradeState.OPEN
        stop = _option_snapshot(
            opened.legs[0].contract,
            market=f.quote(bid=f.price("1.00"), ask=f.price("1.05")),
            times=f.snapshot_times(
                event_time=SLOT_1100,
                source_time=SLOT_1100,
                receive_time=SLOT_1100 + timedelta(milliseconds=50),
                calculation_time=SLOT_1100 + timedelta(milliseconds=120),
            ),
        )
        clock.set(SLOT_1100)
        events = runner.manage_exits({symbol: stop})
        assert events
        closed = runner.trade_manager.get_position(opened.trade_id)
        assert closed is not None
        assert closed.state is TradeState.CLOSED
        assert not store.has_review_slot_run(ReviewSlotId.NSE_AFTERNOON, SESSION_DATE)

    def test_review_full_exit_submits_once(
        self, store: TradingStore, clock: FrozenClock
    ) -> None:
        runner = _open_reviewable_long(store, clock)
        opened = runner.trade_manager.list_positions()[0]
        symbol = opened.legs[0].contract.symbol
        near_expiry = _option_snapshot(
            opened.legs[0].contract,
            market=f.quote(bid=f.price("91.95"), ask=f.price("92.00")),
            derivatives=DerivativesContext(
                days_to_expiry=1,
                open_interest=5000,
                option_type=OptionType.CALL,
                underlying_price=f.price("24000"),
            ),
        )
        first = runner.run_review_slot(
            ReviewSlot(ReviewSlotId.NSE_MORNING, parse_hhmm("10:30")),
            {symbol: near_expiry},
            session_date=SESSION_DATE,
        )
        assert first.decisions
        assert first.decisions[0].action is ReviewAction.FULL_EXIT
        sells_before = [
            event
            for event in runner.broker.list_orders()
            if event.command.side is Side.SELL
        ]
        second = runner.run_review_slot(
            ReviewSlot(ReviewSlotId.NSE_MORNING, parse_hhmm("10:30")),
            {symbol: near_expiry},
            session_date=SESSION_DATE,
        )
        assert second.slot_recorded is False
        sells_after = [
            event
            for event in runner.broker.list_orders()
            if event.command.side is Side.SELL
        ]
        assert len(sells_after) == len(sells_before)


class TestReviewNotify:
    def test_copy_is_advisory_and_software_only(self) -> None:
        review = PositionReviewRecord(
            review_id="REV-1",
            trade_id="TRD-1",
            slot_id=ReviewSlotId.NSE_MORNING,
            session_date=SESSION_DATE,
            action=ReviewAction.HOLD,
            reason_code=ReasonCode.OK,
            detail="frozen policy unchanged; positional review holds",
            submitted=False,
            frozen_policy_id="EXIT-POL-1",
            as_of=SLOT_1030,
        )
        text = format_review_decision(review)
        assert "PAPER review" in text
        assert "promote" not in text.lower()
        assert "broker-resident" in text


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(NOW + timedelta(seconds=60))


@pytest.fixture
def store(tmp_path: Path, clock: FrozenClock) -> Iterator[TradingStore]:
    trading_store = TradingStore.open(tmp_path / "paper.sqlite", clock=clock)
    yield trading_store
    trading_store.close()
