"""ADESK-B5: cold-review scheduler and warm_cold_divergence."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from trading.ai.cold_review import (
    ColdReviewPair,
    maybe_run_cold_review,
    should_run_cold,
    warm_cold_divergence,
)
from trading.ai.decision_log import DecisionLog
from trading.ai.packets import build_delta_packet
from trading.domain.clock import FrozenClock
from trading.domain.enums import DeskRole, ReviewAction, ReviewSlotId
from trading.storage.trading_store import TradingStore

NOW = datetime(2026, 9, 20, 5, 0, tzinfo=UTC)


def test_every_fifth_review_runs_cold() -> None:
    assert should_run_cold(5) is True
    assert should_run_cold(10) is True
    assert should_run_cold(4) is False
    assert should_run_cold(1) is False


def test_divergence_metric() -> None:
    packet = build_delta_packet(
        role=DeskRole.POSITION,
        as_of=NOW,
        snapshot_id="s",
        trade_id="t",
    )
    warm = __import__(
        "trading.ai.position", fromlist=["build_shadow_position_advice"]
    ).build_shadow_position_advice(
        packet, slot_id=ReviewSlotId.NSE_MORNING, deterministic_action=ReviewAction.HOLD
    )
    cold = __import__(
        "trading.ai.position", fromlist=["build_shadow_position_advice"]
    ).build_shadow_position_advice(
        packet,
        slot_id=ReviewSlotId.NSE_MORNING,
        deterministic_action=ReviewAction.FULL_EXIT,
    )
    pairs = (
        ColdReviewPair(5, warm, warm, diverged=False),
        ColdReviewPair(10, warm, cold, diverged=True),
    )
    report = warm_cold_divergence(pairs)
    assert report.pairs == 2
    assert report.divergences == 1
    assert report.warm_cold_divergence == Decimal("0.5000")


def test_maybe_run_logs_both_when_due(tmp_path: Path) -> None:
    store = TradingStore.open(tmp_path / "c.sqlite", clock=FrozenClock(NOW))
    log = DecisionLog(store)
    packet = build_delta_packet(
        role=DeskRole.POSITION, as_of=NOW, snapshot_id="s", trade_id="t"
    )
    assert (
        maybe_run_cold_review(
            packet,
            review_index=3,
            slot_id=ReviewSlotId.NSE_MORNING,
            warm_deterministic=ReviewAction.HOLD,
            cold_deterministic=ReviewAction.HOLD,
            decision_log=log,
            enabled=True,
            run_id="r",
        )
        is None
    )
    pair = maybe_run_cold_review(
        packet,
        review_index=5,
        slot_id=ReviewSlotId.NSE_MORNING,
        warm_deterministic=ReviewAction.HOLD,
        cold_deterministic=ReviewAction.TIGHTEN_STOP,
        decision_log=log,
        enabled=True,
        run_id="r",
    )
    assert pair is not None
    assert pair.diverged is True
    assert len(log.list(role=DeskRole.POSITION)) == 2
    store.close()
