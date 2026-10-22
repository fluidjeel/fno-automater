"""M2 A/B study: logging Mode A closes from lifecycle records must not crash.

Regression: ``_flush_mode_a_closes`` read ``record.state`` (absent on
PositionLifecycleRecord) and ``leg.state`` (absent on PositionLegState),
raising AttributeError in the live paper session.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import tests.factories as f
from trading.domain.clock import FrozenClock
from trading.domain.contracts import PositionLifecycleRecord
from trading.domain.enums import HoldingStyle, ModeId, TradeState
from trading.runtime.m2_ab_study import M2AbStudy
from trading.storage.trading_store import TradingStore

ROOT = Path(__file__).parents[1]


def _record(trade_id: str, state: TradeState, mode: ModeId) -> PositionLifecycleRecord:
    intent = f.intent()
    position = f.position_state(
        trade_id=trade_id,
        intent_id=intent.intent_id,
        state=state,
        exit_policy=f.exit_policy(trade_id=trade_id),
    )
    return PositionLifecycleRecord(
        trade_id=trade_id,
        position=position,
        intent=intent,
        risk_decision=f.risk_decision(intent_id=intent.intent_id),
        holding_style=HoldingStyle.INTRADAY,
        exit_order_ids=("ORD-EXIT-1",) if state is TradeState.CLOSED else (),
        as_of=position.as_of,
        mode_id=mode,
    )


def test_flush_mode_a_closes_logs_closed_m2_only(tmp_path: Path) -> None:
    (tmp_path / "config").mkdir()
    for name in ("m2_ab_study.yaml", "risk.yaml"):
        shutil.copy(ROOT / "config" / name, tmp_path / "config" / name)
    store = TradingStore.open(tmp_path / "paper.sqlite", clock=FrozenClock(f.NOW))
    try:
        store.upsert_position_lifecycle(
            _record("TRD-M2-CLOSED", TradeState.CLOSED, ModeId.M2_DIRECTIONAL),
            event_id="PLC-1",
        )
        store.upsert_position_lifecycle(
            _record("TRD-M2-OPEN", TradeState.OPEN, ModeId.M2_DIRECTIONAL),
            event_id="PLC-2",
        )
        store.upsert_position_lifecycle(
            _record("TRD-M3-CLOSED", TradeState.CLOSED, ModeId.M3_TACTICAL_POSITIONAL),
            event_id="PLC-3",
        )
        study = M2AbStudy.open(tmp_path)
        runner = SimpleNamespace(_services=SimpleNamespace(store=store))

        study._flush_mode_a_closes(runner, now=f.NOW)  # type: ignore[arg-type]
        study._flush_mode_a_closes(runner, now=f.NOW)  # type: ignore[arg-type]

        out = tmp_path / study.results_dir / "results_mode_a.jsonl"
        rows = [json.loads(line) for line in out.read_text().splitlines()]
        assert [row["trade_id"] for row in rows] == ["TRD-M2-CLOSED"]
        assert rows[0]["exit_reason"] == "LAYER2_OR_DISCOVERY_EXIT"
    finally:
        store.close()
