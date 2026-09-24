from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from trading.domain.clock import FrozenClock
from trading.domain.contracts.cycle_evidence import PaperCycleEvidence
from trading.domain.enums import ExecutionMode, ReasonCode, SystemState
from trading.runtime.cycle_evidence import build_cycle_evidence
from trading.runtime.paper_runner import PaperCycleResult, PaperStrategyOutcome
from trading.runtime.session_heartbeat import write_session_heartbeat
from trading.storage.trading_store import TradingEventType, TradingStore


def test_build_and_persist_cycle_evidence(tmp_path: Path) -> None:
    clock = FrozenClock(datetime(2026, 9, 22, 5, 0, tzinfo=UTC))
    store = TradingStore.open(tmp_path / "trading.sqlite", clock=clock)
    result = PaperCycleResult(
        system_state=SystemState.READY,
        outcomes=(
            PaperStrategyOutcome(
                strategy_id="positional_long_option",
                snapshot_id="SNAP-1",
                intents=(),
                rejection_reasons=(ReasonCode.DATA_STALE,),
                decisions=(),
                order_events=(),
                entry_blocked_reasons=(),
                executed=False,
                execution_mode=ExecutionMode.SHADOW,
            ),
        ),
        reconcile_id="REC-1",
        entries_blocked=False,
        route_decision=None,
    )
    evidence = build_cycle_evidence(
        result,
        cycle_id="CYC-1",
        as_of=clock.now_utc(),
    )
    assert evidence.strategies[0].strategy_id == "positional_long_option"
    store.append(
        TradingEventType.CYCLE_EVIDENCE,
        evidence,
        event_id="CYC-1",
    )
    events = store.read_events()
    assert events[-1].event_type is TradingEventType.CYCLE_EVIDENCE
    restored = events[-1].deserialize()
    assert isinstance(restored, PaperCycleEvidence)
    assert restored.cycle_id == "CYC-1"


def test_session_heartbeat_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "session_heartbeat.json"
    write_session_heartbeat(
        path,
        {
            "timestamp": "2026-09-22T05:00:00+00:00",
            "cycle_count": 3,
            "route_winner": "positional_long_option",
        },
    )
    loaded = path.read_text(encoding="utf-8")
    assert "positional_long_option" in loaded
