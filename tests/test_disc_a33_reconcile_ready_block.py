"""DISC-A33: READY + entries_blocked must not crash boot reconciliation or PAPER ticks."""

from __future__ import annotations

from datetime import UTC, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import tests.factories as f
from tests.test_paper_runner import BROKER_FIXTURES, ROOT, _paper_config, _request
from trading.broker.paper import PaperBroker
from trading.broker.ports import BrokerSubmitRequest
from trading.config import load_risk_policy
from trading.domain.clock import FrozenClock
from trading.domain.enums import DifferenceClass, EntryProfile, SystemState
from trading.domain.ids import SequentialIdFactory
from trading.domain.primitives import Currency, Money
from trading.portfolio import PortfolioReconciler
from trading.runtime.paper_runner import PaperRunner
from trading.runtime.paper_session import PaperSession, load_paper_session_config
from trading.storage.trading_store import TradingStore

NOW = datetime(2026, 9, 14, 4, 0, tzinfo=UTC)
IST = ZoneInfo("Asia/Kolkata")
ACCOUNT_ID = "ACC-PAPER-1"


def _submit_request(**overrides: object) -> BrokerSubmitRequest:
    return BrokerSubmitRequest.model_validate(
        {
            "account_id": ACCOUNT_ID,
            "strategy_id": "positional_index_options_poc",
            "order": f.planned_order(),
            **overrides,
        }
    )


def _reconciler(
    broker: PaperBroker,
    store: TradingStore,
    clock: FrozenClock,
    ids: SequentialIdFactory,
) -> PortfolioReconciler:
    return PortfolioReconciler(
        broker,
        store,
        clock=clock,
        id_factory=ids,
        versions=f.versions(),
    )


def test_ready_with_position_drift_transitions_to_degraded_not_exception(
    tmp_path: Path,
) -> None:
    """Invariant 6: blocked entries must not produce READY + entries_blocked."""
    clock = FrozenClock(NOW)
    ids = SequentialIdFactory(clock.instant)
    store = TradingStore.open(tmp_path / "trading.db", clock=clock)
    broker = PaperBroker.from_fixtures(BROKER_FIXTURES, clock=clock, id_factory=ids)
    reconciler = _reconciler(broker, store, clock, ids)

    clean = reconciler.boot_reconcile(ACCOUNT_ID)
    assert clean.result.resulting_system_state is SystemState.READY

    drift_broker = PaperBroker.from_fixtures(
        BROKER_FIXTURES, clock=clock, id_factory=ids
    )
    drift_broker.submit(_submit_request())
    drift_reconciler = _reconciler(drift_broker, store, clock, ids)

    outcome = drift_reconciler.boot_reconcile(ACCOUNT_ID)

    assert outcome.result.prior_system_state is SystemState.READY
    assert outcome.result.entries_blocked is True
    assert outcome.result.resulting_system_state is SystemState.DEGRADED
    assert store.get_system_state()[0] is SystemState.DEGRADED
    assert any(
        event.difference_class is DifferenceClass.UNEXPECTED_BROKER_STATE
        for event in outcome.result.events
    )
    store.close()


def test_paper_session_tick_continues_after_ready_reconcile_block(
    tmp_path: Path,
) -> None:
    """PAPER session must keep exits live when reconciliation blocks entries."""
    clock = FrozenClock(NOW)
    ids = SequentialIdFactory(clock.instant)
    store = TradingStore.open(tmp_path / "paper.sqlite", clock=clock)
    broker = PaperBroker.from_fixtures(BROKER_FIXTURES, clock=clock, id_factory=ids)
    reconciler = _reconciler(broker, store, clock, ids)
    reconciler.boot_reconcile(ACCOUNT_ID)

    broker.submit(_submit_request())
    runner = PaperRunner(
        account_config=_paper_config(),  # type: ignore[arg-type]
        risk_policy=load_risk_policy(ROOT / "config" / "risk.yaml"),
        store=store,
        broker=broker,
        clock=clock,
        id_factory=ids,
    )
    session_cfg = load_paper_session_config(ROOT / "config" / "paper_session.yaml")
    request = _request()
    snapshots = {
        request.candidates[0].contract.symbol: request.candidates[0],
        request.underlying.contract.symbol: request.underlying,
    }

    class _Sink:
        def send(self, _text: str) -> bool:
            return True

    def builder(_now: datetime) -> object:
        return ((request,), snapshots)

    session = PaperSession(
        runner=runner,
        clock=clock,
        session_config=session_cfg,
        session_hours=(time(9, 15), time(15, 30)),
        timezone=IST,
        notifier=_Sink(),
        request_builder=builder,  # type: ignore[arg-type]
        observation_start=NOW,
        capital_limit=Money.of("700000", Currency.INR),
        risk_policy_version="4",
        fill_model_version="conservative-v1",
        code_version="1",
        charges_verified=False,
        cohort_dir=tmp_path / "cohorts",
        account_id=ACCOUNT_ID,
    )
    result = session.tick()
    assert session._cycle_count == 1
    assert result is not None
    assert result.entries_blocked is True
    assert result.system_state is SystemState.DEGRADED
    store.close()


def test_discovery_position_drift_does_not_block_entries(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """DISCOVERY: broker/DB position drift is warning-only."""
    clock = FrozenClock(NOW)
    ids = SequentialIdFactory(clock.instant)
    store = TradingStore.open(tmp_path / "disc.db", clock=clock)
    broker = PaperBroker.from_fixtures(BROKER_FIXTURES, clock=clock, id_factory=ids)
    reconciler = _reconciler(broker, store, clock, ids)
    reconciler.boot_reconcile(ACCOUNT_ID)

    broker.submit(_submit_request())
    with caplog.at_level("WARNING"):
        outcome = reconciler.boot_reconcile(
            ACCOUNT_ID,
            entry_profile=EntryProfile.DISCOVERY,
        )
    assert outcome.result.entries_blocked is False
    assert outcome.result.resulting_system_state is SystemState.READY
    assert any(
        "broker/DB position drift" in record.message for record in caplog.records
    )
    store.close()
