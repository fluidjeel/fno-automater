"""ADESK-B3: POSITION desk SHADOW over delta packets."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from trading.ai.decision_log import DecisionLog
from trading.ai.packets import DeltaPacket, build_delta_packet
from trading.ai.position import (
    build_shadow_position_advice,
    maybe_log_position_shadow,
)
from trading.domain.clock import FrozenClock
from trading.domain.enums import (
    AgentAction,
    AuthorityMode,
    DeskRole,
    GateOutcome,
    ReviewAction,
    ReviewSlotId,
)
from trading.storage.trading_store import TradingStore

NOW = datetime(2026, 9, 20, 5, 0, tzinfo=UTC)  # ~10:30 IST


def _packet() -> DeltaPacket:
    return build_delta_packet(
        role=DeskRole.POSITION,
        as_of=NOW,
        snapshot_id="SNAP-P1",
        trade_id="TRD-P1",
        spot=Decimal("24500"),
        unrealized_r=Decimal("0.4"),
        question="Hold or tighten?",
    )


def test_shadow_mirrors_deterministic_hold() -> None:
    advice = build_shadow_position_advice(
        _packet(),
        slot_id=ReviewSlotId.NSE_MORNING,
        deterministic_action=ReviewAction.HOLD,
    )
    assert advice.action is AgentAction.HOLD
    assert advice.mirrors_deterministic is ReviewAction.HOLD
    assert advice.slot_id is ReviewSlotId.NSE_MORNING


def test_shadow_logged_alongside_deterministic(tmp_path: Path) -> None:
    store = TradingStore.open(tmp_path / "p.sqlite", clock=FrozenClock(NOW))
    log = DecisionLog(store)
    result = maybe_log_position_shadow(
        _packet(),
        slot_id=ReviewSlotId.NSE_AFTERNOON,
        deterministic_action=ReviewAction.TIGHTEN_STOP,
        decision_log=log,
        enabled=True,
        run_id="RUN-P",
    )
    assert result.status == "LOGGED"
    assert result.decision is not None
    assert result.decision.gate_outcome is GateOutcome.SHADOW_ONLY
    assert result.decision.mode is AuthorityMode.SHADOW
    assert result.decision.action is AgentAction.TIGHTEN_STOP
    assert len(log.list(role=DeskRole.POSITION)) == 1
    store.close()


def test_disabled_no_live_influence(tmp_path: Path) -> None:
    store = TradingStore.open(tmp_path / "p2.sqlite", clock=FrozenClock(NOW))
    log = DecisionLog(store)
    result = maybe_log_position_shadow(
        _packet(),
        slot_id=ReviewSlotId.NSE_MORNING,
        deterministic_action=ReviewAction.FULL_EXIT,
        decision_log=log,
        enabled=False,
        run_id="RUN-OFF",
    )
    assert result.status == "SKIPPED_DISABLED"
    assert log.list(role=DeskRole.POSITION) == ()
    store.close()
