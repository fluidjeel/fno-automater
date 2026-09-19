"""PAPER-001 supervised runner isolation and cycle lineage."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

import tests.factories as f
from trading.broker.paper import PaperBroker
from trading.cli import main
from trading.config import (
    Environment,
    load_config_text,
    load_evaluation_config,
    load_risk_policy,
)
from trading.domain.clock import FrozenClock
from trading.domain.contracts import DerivativesContext, InstrumentSpec
from trading.domain.enums import (
    Exchange,
    ExecutionMode,
    InstrumentKind,
    OptionType,
    OrderState,
    ReasonCode,
    RiskAction,
)
from trading.domain.ids import SequentialIdFactory
from trading.runtime import PaperIsolationError, PaperRunner, PaperStrategyRequest
from trading.runtime.isolation import assert_paper_isolation
from trading.storage.trading_store import TradingStore
from trading.strategies import MacroAssessment, MacroBias

ROOT = Path(__file__).resolve().parent.parent
BROKER_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "broker"
NOW = f.NOW
NOW_CTX = NOW + timedelta(seconds=60)


def _paper_config() -> object:
    raw = (ROOT / "config" / "base.yaml").read_text(encoding="utf-8")
    raw = raw.replace("environment: BACKTEST", "environment: PAPER")
    raw = raw.replace('account_id: "REPLACE_ME"', 'account_id: "ACC-PAPER-1"')
    return load_config_text(raw, expect_environment=Environment.PAPER)


def _option_spec() -> InstrumentSpec:
    return InstrumentSpec.model_validate(
        {
            "trading_symbol": "NSE:NIFTY26SEP24000CE",
            "exchange": Exchange.NFO,
            "segment": "NSE_FO",
            "underlying": "NIFTY",
            "instrument_kind": InstrumentKind.OPTION,
            "provider_token": "1",
            "exchange_token": 1,
            "lot_size": 75,
            "tick_size": Decimal("0.05"),
            "price_precision": 2,
            "expiry": date(2026, 9, 24),
            "strike": Decimal("24000"),
            "option_type": OptionType.CALL,
            "trading_session": "0915-1540",
            "source": "fixture",
            "verified_at": date(2026, 9, 11),
        }
    )


def _request(**overrides: object) -> PaperStrategyRequest:
    option = f.snapshot(
        snapshot_id="SNAP-OPT",
        contract=f.option_contract(),
        market=f.quote(
            bid=f.price("91.95"),
            ask=f.price("92.00"),
            last=f.price("92.00"),
            bid_size=300,
            ask_size=300,
        ),
        derivatives=DerivativesContext(
            days_to_expiry=10,
            open_interest=5000,
            option_type=OptionType.CALL,
            underlying_price=f.price("24000"),
        ),
    )
    payload: dict[str, object] = {
        "strategy_id": "positional_long_option",
        "underlying": f.snapshot(
            snapshot_id="SNAP-UNDER",
            contract=f.index_contract(),
            market=f.quote(last=f.price("24050"), close=f.price("24000")),
        ),
        "candidates": (option,),
        "instruments": {option.contract.symbol: _option_spec()},
        "event_risk_state": f.event_risk_state(),
        "experiment_id": "EXP-PAPER-LO-1",
        "macro": MacroAssessment(
            regime="RISK_ON",
            directional_bias=MacroBias.BULLISH,
            confidence=Decimal("0.8"),
            fresh_until=NOW_CTX + timedelta(hours=1),
            model_version="test",
        ),
    }
    payload.update(overrides)
    return PaperStrategyRequest(**payload)  # type: ignore[arg-type]


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(NOW_CTX)


@pytest.fixture
def store(tmp_path: Path, clock: FrozenClock) -> Iterator[TradingStore]:
    trading_store = TradingStore.open(tmp_path / "paper.sqlite", clock=clock)
    yield trading_store
    trading_store.close()


class TestPaperIsolation:
    def test_live_environment_is_refused(self, clock: FrozenClock) -> None:
        """PAPER environment cannot submit through a live broker."""
        broker = PaperBroker.from_fixtures(
            BROKER_FIXTURES,
            clock=clock,
            id_factory=SequentialIdFactory(clock.instant),
        )
        with pytest.raises(PaperIsolationError, match=r"Environment\.PAPER"):
            assert_paper_isolation(Environment.LIVE, ExecutionMode.PAPER, broker)

    def test_real_capital_mode_is_refused(self, clock: FrozenClock) -> None:
        broker = PaperBroker.from_fixtures(
            BROKER_FIXTURES,
            clock=clock,
            id_factory=SequentialIdFactory(clock.instant),
        )
        with pytest.raises(PaperIsolationError, match="real capital"):
            assert_paper_isolation(Environment.PAPER, ExecutionMode.CANARY_REAL, broker)

    def test_fyers_transaction_module_is_refused(self) -> None:
        class _FyersShaped:
            __module__ = "trading.broker.fyers.adapter"

        with pytest.raises(PaperIsolationError, match="live transaction"):
            assert_paper_isolation(
                Environment.PAPER, ExecutionMode.PAPER, _FyersShaped()
            )

    def test_cli_isolate_check_rejects_backtest_config(self) -> None:
        assert main(["paper", "isolate-check", "--config", "config/base.yaml"]) == 1


class TestPaperCycle:
    def test_unknown_event_state_blocks_with_reason(
        self, store: TradingStore, clock: FrozenClock
    ) -> None:
        """Unknown event-risk state blocks new exposure with a reason code."""
        ids = SequentialIdFactory(clock.instant)
        fill_model = load_evaluation_config(
            ROOT / "config" / "evaluation.yaml"
        ).config.fill_model
        broker = PaperBroker.from_fixtures(
            BROKER_FIXTURES, clock=clock, id_factory=ids, fill_model=fill_model
        )
        runner = PaperRunner(
            account_config=_paper_config(),  # type: ignore[arg-type]
            risk_policy=load_risk_policy(ROOT / "config" / "risk.yaml"),
            store=store,
            broker=broker,
            clock=clock,
            id_factory=ids,
            fill_model=fill_model,
        )
        result = runner.run_cycle((_request(event_risk_state=None),))
        outcome = result.outcomes[0]
        assert outcome.order_events == ()
        reasons = outcome.rejection_reasons + tuple(
            code for decision in outcome.decisions for code in decision.reason_codes
        )
        assert reasons
        assert ReasonCode.EVENT_BLACKOUT in reasons or outcome.decisions

    def test_unlisted_strategy_is_capital_unavailable(
        self, store: TradingStore, clock: FrozenClock
    ) -> None:
        ids = SequentialIdFactory(clock.instant)
        broker = PaperBroker.from_fixtures(BROKER_FIXTURES, clock=clock, id_factory=ids)
        runner = PaperRunner(
            account_config=_paper_config(),  # type: ignore[arg-type]
            risk_policy=load_risk_policy(ROOT / "config" / "risk.yaml"),
            store=store,
            broker=broker,
            clock=clock,
            id_factory=ids,
        )
        result = runner.run_cycle((_request(strategy_id="not_a_strategy"),))
        assert result.outcomes[0].rejection_reasons == (ReasonCode.INSTRUMENT_UNKNOWN,)
        assert result.outcomes[0].order_events == ()

    def test_cycle_lineage_records_risk_and_orders(
        self, store: TradingStore, clock: FrozenClock
    ) -> None:
        ids = SequentialIdFactory(clock.instant)
        fill_model = load_evaluation_config(
            ROOT / "config" / "evaluation.yaml"
        ).config.fill_model
        broker = PaperBroker.from_fixtures(
            BROKER_FIXTURES, clock=clock, id_factory=ids, fill_model=fill_model
        )
        runner = PaperRunner(
            account_config=_paper_config(),  # type: ignore[arg-type]
            risk_policy=load_risk_policy(ROOT / "config" / "risk.yaml"),
            store=store,
            broker=broker,
            clock=clock,
            id_factory=ids,
            fill_model=fill_model,
        )
        result = runner.run_cycle((_request(),))
        outcome = result.outcomes[0]
        assert outcome.intents
        assert outcome.decisions
        assert outcome.decisions[0].action in {RiskAction.APPROVE, RiskAction.RESIZE}
        assert outcome.order_events
        assert outcome.order_events[0].state is OrderState.FILLED
        assert outcome.order_events[0].identity.experiment_id == "EXP-PAPER-LO-1"
        stored = [event.event_type.value for event in store.read_events()]
        assert "risk_decision" in stored
        assert "order_event" in stored

    def test_cas_without_microstructure_features_does_not_submit(
        self, store: TradingStore, clock: FrozenClock
    ) -> None:
        ids = SequentialIdFactory(clock.instant)
        broker = PaperBroker.from_fixtures(BROKER_FIXTURES, clock=clock, id_factory=ids)
        runner = PaperRunner(
            account_config=_paper_config(),  # type: ignore[arg-type]
            risk_policy=load_risk_policy(ROOT / "config" / "risk.yaml"),
            store=store,
            broker=broker,
            clock=clock,
            id_factory=ids,
        )
        result = runner.run_cycle((_request(strategy_id="cas_microstructure"),))
        outcome = result.outcomes[0]
        assert outcome.order_events == ()
        assert outcome.intents == ()
