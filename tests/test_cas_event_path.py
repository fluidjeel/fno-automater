"""CAS event-driven path latency and gating (Gate G3)."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import tests.factories as f
from trading.domain.clock import FrozenClock
from trading.domain.contracts import DerivativesContext
from trading.domain.enums import ExecutionMode, OptionType, ReasonCode
from trading.runtime.cas_event_path import (
    CasEventDrivenConfig,
    cas_event_paper_permitted,
    evaluate_cas_event_trigger,
    measure_cas_entry_latency,
)

NOW = f.NOW


def test_cas_event_trigger_requires_fresh_quote() -> None:
    clock = FrozenClock(NOW)
    candidate = f.snapshot(
        snapshot_id="CAS-1",
        contract=f.option_contract(symbol="NIFTY26OCT24000CE"),
        market=f.quote(
            bid=f.price("49.95"),
            ask=f.price("50.05"),
            last=f.price("50.00"),
            bid_size=100,
            ask_size=200,
        ),
        derivatives=DerivativesContext(
            days_to_expiry=5,
            open_interest=5000,
            option_type=OptionType.CALL,
            underlying_price=f.price("24000"),
        ),
    )
    config = CasEventDrivenConfig(
        enabled=True,
        profile="quote_only",
        quote_max_age_ms=500,
        microprice_dislocation_ticks=Decimal("0.01"),
    )
    event_at = clock.now_utc()
    result = evaluate_cas_event_trigger(
        candidate,
        config=config,
        event_at=event_at,
        now=event_at + timedelta(milliseconds=100),
    )
    assert result.triggered
    stale = evaluate_cas_event_trigger(
        candidate,
        config=config,
        event_at=event_at,
        now=event_at + timedelta(seconds=2),
    )
    assert not stale.triggered
    assert ReasonCode.DATA_STALE in stale.reason_codes


def test_cas_paper_requires_latency_gate() -> None:
    config = CasEventDrivenConfig(enabled=True, max_entry_latency_ms=1000)
    report = measure_cas_entry_latency((50, 80, 1500), config=config)
    assert not report.passes_session_gate
    ok = measure_cas_entry_latency((50, 80, 90), config=config)
    assert ok.passes_session_gate
    permitted, mode = cas_event_paper_permitted(
        config=config,
        latency_report=ok,
        stance=ExecutionMode.PAPER,
        attempts_today=0,
    )
    assert permitted
    assert mode is ExecutionMode.PAPER
