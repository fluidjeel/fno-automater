"""ADESK-C2: freeze TerminalPolicy into ExitPolicy; restart preserve.

PositionLifecycleRecord JSON round-trip is the PAPER restart path. Stops must
never widen. Replacing an already-frozen terminal_policy is refused.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

import tests.factories as f
from trading.domain.clock import FrozenClock
from trading.domain.contracts.position import ExitPolicy
from trading.domain.contracts.terminal_policy import TerminalPolicy
from trading.domain.contracts.trade_thesis import InvalidationCondition
from trading.domain.enums import (
    Comparator,
    InvalidationMetric,
    InvalidationSeverity,
    TerminalPolicyKind,
)
from trading.domain.primitives import Currency, Money
from trading.storage.trading_store import TradingStore
from trading.trade.exits import attach_terminal_policy, tighten_exit_policy

NOW = datetime(2026, 9, 20, 10, 0, tzinfo=UTC)
INR = Currency.INR


def _money(v: str) -> Money:
    return Money.of(v, INR)


def _run_condition() -> InvalidationCondition:
    return InvalidationCondition(
        condition_id="RC-1",
        metric=InvalidationMetric.SPOT_PCT_FROM_ENTRY,
        comparator=Comparator.LT,
        threshold=Decimal("-3"),
        severity=InvalidationSeverity.HARD,
    )


def _terminal(trade_id: str = "TRD-1") -> TerminalPolicy:
    return TerminalPolicy(
        policy_id="TP-1",
        trade_id=trade_id,
        kind=TerminalPolicyKind.RUN_TO_EXPIRY_DEFINED_RISK,
        flatten_dte=None,
        run_conditions=(_run_condition(),),
        max_terminal_loss=_money("5000"),
        accepted_at=NOW,
        requested_kind=TerminalPolicyKind.RUN_TO_EXPIRY_DEFINED_RISK,
        reject_reasons=(),
        max_loss_hallucination=False,
    )


def test_attach_freezes_without_changing_stops() -> None:
    base = f.exit_policy()
    frozen = attach_terminal_policy(base, _terminal(base.trade_id))
    assert frozen.terminal_policy is not None
    assert frozen.terminal_policy.kind is TerminalPolicyKind.RUN_TO_EXPIRY_DEFINED_RISK
    assert frozen.terminal_policy.run_conditions[0].condition_id == "RC-1"
    assert frozen.current_stop_distance_ticks == base.current_stop_distance_ticks
    assert frozen.stop_price == base.stop_price
    assert frozen.initial_stop_distance_ticks == base.initial_stop_distance_ticks


def test_attach_refuses_replace() -> None:
    base = attach_terminal_policy(f.exit_policy(), _terminal())
    with pytest.raises(ValueError, match="already carries"):
        attach_terminal_policy(base, _terminal())


def test_attach_trade_id_mismatch() -> None:
    with pytest.raises(ValueError, match="trade_id"):
        attach_terminal_policy(f.exit_policy(), _terminal("OTHER"))


def test_stops_never_widen_still_enforced_with_terminal() -> None:
    base = attach_terminal_policy(f.exit_policy(), _terminal())
    with pytest.raises(ValidationError):
        payload = {**base.model_dump(mode="json"), "current_stop_distance_ticks": 500}
        ExitPolicy.model_validate(payload)


def test_tighten_preserves_terminal_policy() -> None:
    base = attach_terminal_policy(
        f.exit_policy(
            stop_price=f.price("95.00"),
            initial_stop_distance_ticks=200,
            current_stop_distance_ticks=200,
        ),
        _terminal(),
    )
    template = f.exit_template(break_even_trigger_ticks=10)
    entry = f.price("100.00")
    monitor = f.price("101.00")
    tightened = tighten_exit_policy(
        base, template, entry_price=entry, monitor_price=monitor
    )
    assert tightened is not None
    assert tightened.terminal_policy == base.terminal_policy
    assert tightened.breakeven_active is True


def test_lifecycle_store_restart_preserves_policy_and_run_conditions(
    tmp_path: Path,
) -> None:
    """Restart path: upsert PositionLifecycleRecord → reload → ExitPolicy intact."""
    clock = FrozenClock(NOW)
    store = TradingStore.open(tmp_path / "c2.sqlite", clock=clock)
    try:
        position = f.position_state(
            exit_policy=attach_terminal_policy(f.exit_policy(), _terminal())
        )
        record = f.position_lifecycle_record(position=position)
        store.upsert_position_lifecycle(record, event_id="PLC-C2-1")
        restored = store.get_position_lifecycle(position.trade_id)
        assert restored is not None
        tp = restored.position.exit_policy.terminal_policy
        assert tp is not None
        assert tp.kind is TerminalPolicyKind.RUN_TO_EXPIRY_DEFINED_RISK
        assert len(tp.run_conditions) == 1
        assert tp.run_conditions[0].condition_id == "RC-1"
        assert tp.run_conditions[0].metric is InvalidationMetric.SPOT_PCT_FROM_ENTRY
        assert (
            restored.position.exit_policy.stop_price == position.exit_policy.stop_price
        )
    finally:
        store.close()
