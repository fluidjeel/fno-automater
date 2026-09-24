"""Phase P13: positional reviews, missed-slot recovery, and G2 roll/switch gating."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tests.test_paper_lifecycle import _open_long, _option_snapshot
from tests.test_paper_review import (
    SESSION_DATE,
    SLOT_1430,
    _engine_inputs,
    _nse_slots,
    _open_reviewable_long,
    _stamp_carry_approved,
)
from tests.test_paper_runner import ROOT, _paper_config
from trading.broker.paper import PaperBroker
from trading.config import load_risk_policy
from trading.domain.clock import FrozenClock
from trading.domain.enums import (
    ModeId,
    ReasonCode,
    ReviewAction,
    ReviewExecutionStatus,
    ReviewSlotId,
)
from trading.domain.ids import SequentialIdFactory
from trading.runtime.paper_runner import (
    PaperRunner,
    _eligible_for_scheduled_review,
    _resolve_review_evaluation,
)
from trading.runtime.review_schedule import ReviewSlot, due_review_slots, parse_hhmm
from trading.storage.trading_store import TradingStore
from trading.trade.review_roll_switch import family_supports_roll_switch

NOW = datetime(2026, 9, 14, 4, 0, tzinfo=UTC)
IST = ZoneInfo("Asia/Kolkata")


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(NOW + timedelta(seconds=60))


@pytest.fixture
def store(tmp_path: Path, clock: FrozenClock) -> Iterator[TradingStore]:
    trading_store = TradingStore.open(tmp_path / "paper.sqlite", clock=clock)
    yield trading_store
    trading_store.close()


def _runner(store: TradingStore, clock: FrozenClock) -> PaperRunner:
    ids = SequentialIdFactory(clock.instant)
    broker = PaperBroker.from_fixtures(
        Path(__file__).resolve().parent / "fixtures" / "broker",
        clock=clock,
        id_factory=ids,
    )
    return PaperRunner(
        account_config=_paper_config(),  # type: ignore[arg-type]
        risk_policy=load_risk_policy(ROOT / "config" / "risk.yaml"),
        store=store,
        broker=broker,
        clock=clock,
        id_factory=ids,
    )


class TestMissedSlotRecovery:
    def test_t39_both_missed_slots_collapse_to_one_recovery(self) -> None:
        afternoon = SLOT_1430.astimezone(IST)
        recovery = due_review_slots(
            now_local=afternoon,
            session_open=time(9, 15),
            eod=time(15, 40),
            slots=_nse_slots(),
            recorded=frozenset(),
        )
        assert len(recovery) == 1
        assert recovery[0].is_recovery is True
        assert recovery[0].missed_slot_ids == (
            ReviewSlotId.NSE_MORNING,
            ReviewSlotId.NSE_AFTERNOON,
        )

    def test_recovery_records_every_missed_slot_once(
        self, store: TradingStore, clock: FrozenClock
    ) -> None:
        runner = _open_reviewable_long(store, clock)
        opened = runner.trade_manager.list_positions()[0]
        symbol = opened.legs[0].contract.symbol
        snapshot = _option_snapshot(opened.legs[0].contract)
        result = runner.run_review_slot(
            ReviewSlot(ReviewSlotId.NSE_AFTERNOON, parse_hhmm("14:30")),
            {symbol: snapshot},
            session_date=SESSION_DATE,
            missed_slot_ids=(
                ReviewSlotId.NSE_MORNING,
                ReviewSlotId.NSE_AFTERNOON,
            ),
            configured_slots=_nse_slots(),
        )
        assert result.is_recovery is True
        assert store.has_review_slot_run(ReviewSlotId.NSE_MORNING, SESSION_DATE)
        assert store.has_review_slot_run(ReviewSlotId.NSE_AFTERNOON, SESSION_DATE)
        lifecycle = store.get_position_lifecycle(opened.trade_id)
        assert lifecycle is not None
        assert len(lifecycle.reviews) == 1
        review = lifecycle.reviews[0]
        assert review.missed_slot_ids == (
            ReviewSlotId.NSE_MORNING,
            ReviewSlotId.NSE_AFTERNOON,
        )
        duplicate = runner.run_review_slot(
            ReviewSlot(ReviewSlotId.NSE_AFTERNOON, parse_hhmm("14:30")),
            {symbol: snapshot},
            session_date=SESSION_DATE,
            missed_slot_ids=(
                ReviewSlotId.NSE_MORNING,
                ReviewSlotId.NSE_AFTERNOON,
            ),
            configured_slots=_nse_slots(),
        )
        assert duplicate.slot_recorded is False
        assert duplicate.decisions == ()


class TestRollSwitchGating:
    def test_g2_long_call_roll_resolves_to_close_path(self) -> None:
        position, intent, _feature = _engine_inputs(dte=1, expiry_days=None)
        intent = intent.model_copy(update={"family_id": "long_call"})
        evaluation, status, _next = _resolve_review_evaluation(
            ReviewEvaluationStub.propose_roll(),
            intent=intent,
            position=position,
            slot=ReviewSlot(ReviewSlotId.NSE_MORNING, parse_hhmm("10:30")),
            configured_slots=_nse_slots(),
        )
        assert evaluation.action is ReviewAction.ROLL
        assert evaluation.exit_quantity_contracts == 75
        assert status is ReviewExecutionStatus.NOT_APPLICABLE

    def test_unproven_family_roll_is_proposed_not_executed(self) -> None:
        position, intent, _feature = _engine_inputs(dte=1, expiry_days=None)
        intent = intent.model_copy(update={"family_id": "credit_spread"})
        evaluation, status, _next = _resolve_review_evaluation(
            ReviewEvaluationStub.propose_roll(),
            intent=intent,
            position=position,
            slot=ReviewSlot(ReviewSlotId.NSE_MORNING, parse_hhmm("10:30")),
            configured_slots=_nse_slots(),
        )
        assert evaluation.action is ReviewAction.PROPOSE_ROLL
        assert evaluation.reason_code is ReasonCode.PROPOSED_NOT_EXECUTED
        assert status is ReviewExecutionStatus.PROPOSED_NOT_EXECUTED
        assert not family_supports_roll_switch("credit_spread")

    def test_g2_roll_close_path_submits_exit(
        self, store: TradingStore, clock: FrozenClock
    ) -> None:
        from trading.trade.review import ReviewEvaluation

        runner = _open_reviewable_long(store, clock)
        opened = runner.trade_manager.list_positions()[0]
        book = runner._open_book[opened.trade_id]
        intent, decision = book
        symbol = opened.legs[0].contract.symbol
        snapshot = _option_snapshot(opened.legs[0].contract)
        evaluation = ReviewEvaluation(
            action=ReviewAction.ROLL,
            reason_code=ReasonCode.OK,
            detail="G2 roll close path",
            exit_quantity_contracts=opened.legs[0].quantity_contracts,
        )
        submitted = runner._apply_review(
            evaluation,
            intent=intent,
            decision=decision,
            position=opened,
            snapshots={symbol: snapshot},
        )
        assert submitted is True


class TestModeEligibility:
    def test_m1_is_excluded_from_scheduled_review(self) -> None:
        position, intent, _feature = _engine_inputs()
        intent = intent.model_copy(update={"mode_id": ModeId.M1_CAS})
        position = position.model_copy(update={"mode_id": ModeId.M1_CAS})
        assert _eligible_for_scheduled_review(position, intent, None) is False

    def test_m2_requires_carry_approval(
        self, store: TradingStore, clock: FrozenClock
    ) -> None:
        runner = _open_long(store, clock)
        opened = runner.trade_manager.list_positions()[0]
        lifecycle = store.get_position_lifecycle(opened.trade_id)
        assert lifecycle is not None
        assert (
            _eligible_for_scheduled_review(opened, lifecycle.intent, lifecycle) is False
        )
        approved = _stamp_carry_approved(runner, opened.trade_id)
        assert _eligible_for_scheduled_review(opened, approved.intent, approved) is True

    def test_m3_is_always_eligible(self) -> None:
        position, intent, _feature = _engine_inputs()
        intent = intent.model_copy(update={"mode_id": ModeId.M3_TACTICAL_POSITIONAL})
        position = position.model_copy(
            update={"mode_id": ModeId.M3_TACTICAL_POSITIONAL}
        )
        assert _eligible_for_scheduled_review(position, intent, None) is True


class TestHoldNextSlot:
    def test_hold_records_next_slot_time(self) -> None:
        position, intent, _feature = _engine_inputs()
        evaluation, status, next_slot = _resolve_review_evaluation(
            ReviewEvaluationStub.hold(),
            intent=intent,
            position=position,
            slot=ReviewSlot(ReviewSlotId.NSE_MORNING, parse_hhmm("10:30")),
            configured_slots=_nse_slots(),
        )
        assert evaluation.action is ReviewAction.HOLD
        assert next_slot is ReviewSlotId.NSE_AFTERNOON
        assert "next review slot NSE_AFTERNOON" in evaluation.detail
        assert status is ReviewExecutionStatus.NOT_APPLICABLE


class ReviewEvaluationStub:
    @staticmethod
    def propose_roll() -> object:
        from trading.trade.review import ReviewEvaluation

        return ReviewEvaluation(
            action=ReviewAction.PROPOSE_ROLL,
            reason_code=ReasonCode.REVIEW_PROPOSAL_REQUIRES_L2,
            detail="near expiry with no frozen flatten",
        )

    @staticmethod
    def hold() -> object:
        from trading.trade.review import ReviewEvaluation

        return ReviewEvaluation(
            action=ReviewAction.HOLD,
            reason_code=ReasonCode.OK,
            detail="frozen policy unchanged; positional review holds",
        )
