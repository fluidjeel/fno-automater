"""PAPER-003/004: unattended session, isolation, restart, event-risk, Telegram copy."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import tests.factories as f
from tests.test_paper_runner import BROKER_FIXTURES, ROOT, _paper_config, _request
from trading.broker.paper import PaperBroker
from trading.broker.ports import BrokerSubmitRequest
from trading.cli import main
from trading.config import (
    Environment,
    load_config,
    load_evaluation_config,
    load_risk_policy,
)
from trading.domain.clock import FrozenClock
from trading.domain.contracts import CohortPackage
from trading.domain.enums import DataQuality, ExecutionMode, ReasonCode
from trading.domain.ids import SequentialIdFactory
from trading.domain.primitives import Currency, Money
from trading.runtime.event_risk import clear_event_risk
from trading.runtime.isolation import PaperIsolationError, assert_paper_isolation
from trading.runtime.notify import format_eod_report, format_post_trade
from trading.runtime.paper_runner import PaperRunner, PaperStrategyOutcome
from trading.runtime.paper_session import PaperSession, load_paper_session_config
from trading.storage.trading_store import TradingStore

NOW = datetime(2026, 9, 14, 4, 0, tzinfo=UTC)
EOD = datetime(2026, 9, 14, 10, 10, tzinfo=UTC)
IST = ZoneInfo("Asia/Kolkata")


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(NOW)


@pytest.fixture
def store(tmp_path: Path, clock: FrozenClock) -> Iterator[TradingStore]:
    trading_store = TradingStore.open(tmp_path / "paper.sqlite", clock=clock)
    yield trading_store
    trading_store.close()


class _Sink:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def send(self, text: str) -> bool:
        self.messages.append(text)
        return True


def _runner(store: TradingStore, clock: FrozenClock) -> PaperRunner:
    ids = SequentialIdFactory(clock.instant)
    fill_model = load_evaluation_config(
        ROOT / "config" / "evaluation.yaml"
    ).config.fill_model
    broker = PaperBroker.from_fixtures(
        BROKER_FIXTURES, clock=clock, id_factory=ids, fill_model=fill_model
    )
    return PaperRunner(
        account_config=_paper_config(),  # type: ignore[arg-type]
        risk_policy=load_risk_policy(ROOT / "config" / "risk.yaml"),
        store=store,
        broker=broker,
        clock=clock,
        id_factory=ids,
        fill_model=fill_model,
    )


class TestIsolationAndConfig:
    def test_paper_yaml_is_paper_environment(self) -> None:
        loaded = load_config(ROOT / "config" / "paper.yaml")
        assert loaded.config.environment is Environment.PAPER
        assert loaded.config.account_id == "ACC-PAPER-1"

    def test_cli_isolate_check_accepts_paper_yaml(self) -> None:
        assert main(["paper", "isolate-check", "--config", "config/paper.yaml"]) == 0

    def test_fyers_transaction_adapter_still_refused(self) -> None:
        class _FyersShaped:
            __module__ = "trading.broker.fyers.adapter"

        with pytest.raises(PaperIsolationError, match="live transaction"):
            assert_paper_isolation(
                Environment.PAPER, ExecutionMode.PAPER, _FyersShaped()
            )


class TestEventRiskAndStale:
    def test_missing_event_risk_does_not_submit(
        self, store: TradingStore, clock: FrozenClock
    ) -> None:
        runner = _runner(store, clock)
        result = runner.run_cycle((_request(event_risk_state=None),))
        outcome = result.outcomes[0]
        assert outcome.order_events == ()
        reasons = outcome.rejection_reasons + tuple(
            code for decision in outcome.decisions for code in decision.reason_codes
        )
        assert ReasonCode.EVENT_BLACKOUT in reasons or not outcome.intents

    def test_stale_snapshot_blocks_entries(
        self, store: TradingStore, clock: FrozenClock
    ) -> None:
        runner = _runner(store, clock)
        stale = f.snapshot(
            quality=f.quality(
                state=DataQuality.STALE, reason_codes=(ReasonCode.DATA_STALE,)
            )
        )
        request = _request()
        patched = replace(request, underlying=stale, candidates=(stale,))
        result = runner.run_cycle((patched,))
        assert result.outcomes[0].order_events == ()

    def test_clear_event_risk_is_global_normal(self) -> None:
        state = clear_event_risk(as_of=NOW, ttl_seconds=60)
        assert state.scope == "GLOBAL"
        assert state.state.value == "NORMAL"


class TestRestartIdempotency:
    def test_reloaded_broker_does_not_double_submit(self, clock: FrozenClock) -> None:
        ids = SequentialIdFactory(clock.instant)
        first = PaperBroker.from_fixtures(BROKER_FIXTURES, clock=clock, id_factory=ids)
        request = BrokerSubmitRequest.model_validate(
            {
                "account_id": "ACC-PAPER-1",
                "strategy_id": "positional_long_option",
                "order": f.planned_order(),
            }
        )
        event = first.submit(request)
        second = PaperBroker.from_fixtures(
            BROKER_FIXTURES, clock=clock, id_factory=SequentialIdFactory(clock.instant)
        )
        second.load_state(first.dump_state())
        replay = second.submit(request)
        assert replay.identity.idempotency_key == event.identity.idempotency_key
        assert len(second.get_positions()) == 1


class TestTelegramAndCohort:
    def test_post_trade_copy_is_advisory(self) -> None:
        outcome = PaperStrategyOutcome(
            strategy_id="positional_long_option",
            snapshot_id="SNAP-1",
            intents=(),
            rejection_reasons=(),
            decisions=(),
            order_events=(),
        )
        report = format_eod_report((), open_trade_count=0, charges_verified=False)
        combined = (
            "\n".join(format_post_trade(outcome, experiment_id="EXP-PAPER-LO-2026W38"))
            + report
        )
        assert "promote" not in combined.lower()
        assert "unset" in report.lower() or "unknown" in report.lower()

    def test_session_tick_notifies_and_eod_writes_cohort(
        self, store: TradingStore, clock: FrozenClock, tmp_path: Path
    ) -> None:
        runner = _runner(store, clock)
        sink = _Sink()
        session_cfg = load_paper_session_config(ROOT / "config" / "paper_session.yaml")
        request = _request()
        snapshots = {
            request.candidates[0].contract.symbol: request.candidates[0],
            request.underlying.contract.symbol: request.underlying,
        }

        def builder(_now: datetime) -> object:
            return (request,), snapshots

        session = PaperSession(
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
        result = session.tick()
        assert result is not None
        session._send_eod(clock.now_utc())
        assert sink.messages
        assert all("promote" not in item.lower() for item in sink.messages)
        files = list((tmp_path / "cohorts").glob("*.json"))
        assert files
        payload = files[0].read_text(encoding="utf-8")
        package = CohortPackage.model_validate_json(payload)
        assert package.experiment.execution_mode is ExecutionMode.PAPER

    def test_run_once_after_eod_exits_without_sleep(
        self, store: TradingStore, tmp_path: Path
    ) -> None:
        clock = FrozenClock(EOD)
        runner = _runner(store, clock)
        sink = _Sink()
        slept: list[float] = []
        session_cfg = load_paper_session_config(ROOT / "config" / "paper_session.yaml")
        session = PaperSession(
            runner=runner,
            clock=clock,
            session_config=session_cfg,
            session_hours=(time(9, 15), time(15, 30)),
            timezone=IST,
            notifier=sink,
            request_builder=lambda _now: ((), {}),
            observation_start=NOW,
            capital_limit=Money.of("700000", Currency.INR),
            risk_policy_version="4",
            fill_model_version="conservative-v1",
            code_version="1",
            charges_verified=False,
            cohort_dir=tmp_path / "cohorts",
            sleeper=slept.append,
        )
        assert session.run(once=True) == 0
        assert slept == []
        assert any("EOD" in item for item in sink.messages)
