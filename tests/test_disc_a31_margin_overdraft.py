"""DISC-A31: PAPER broker margin overdraft must not crash exposure or session ticks."""

from __future__ import annotations

from datetime import UTC, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import tests.factories as f
from tests.test_paper_runner import BROKER_FIXTURES, ROOT, _paper_config, _request
from trading.broker.paper import PaperBroker
from trading.config import load_evaluation_config, load_risk_policy
from trading.config.evaluation import FillModelConfig
from trading.domain.clock import FrozenClock
from trading.domain.ids import SequentialIdFactory
from trading.domain.primitives import Currency, Money
from trading.portfolio.risk_journal import PortfolioRiskJournal
from trading.portfolio.snapshot import build_broker_snapshot
from trading.runtime.paper_runner import PaperRunner
from trading.runtime.paper_session import PaperSession, load_paper_session_config

NOW = datetime(2026, 9, 14, 4, 0, tzinfo=UTC)
IST = ZoneInfo("Asia/Kolkata")
ACCOUNT_ID = "ACC-PAPER-1"


def _overdrawn_broker(
    clock: FrozenClock,
    id_factory: SequentialIdFactory,
    *,
    fill_model: FillModelConfig | None = None,
) -> PaperBroker:
    broker = PaperBroker.from_fixtures(
        BROKER_FIXTURES,
        clock=clock,
        id_factory=id_factory,
        fill_model=fill_model,
    )
    funds = broker.get_funds()
    broker.load_state(
        {
            "funds": funds.model_copy(
                update={
                    "equity": Money.of("700000", Currency.INR),
                    "margin_used": Money.of("1422307.25", Currency.INR),
                    "margin_available": Money.of("-722307.25", Currency.INR),
                }
            ).model_dump(mode="json"),
            "positions": [],
            "orders": [],
        }
    )
    return broker


def test_build_broker_snapshot_clamps_margin_overdraft(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Invariant 6: invalid broker margin must not abort portfolio snapshot construction."""
    clock = FrozenClock(NOW)
    ids = SequentialIdFactory(clock.instant)
    broker = _overdrawn_broker(clock, ids)
    with caplog.at_level("WARNING"):
        snapshot = build_broker_snapshot(
            broker,
            account_id=ACCOUNT_ID,
            versions=f.versions(),
            id_factory=ids,
            reserved_capital=f.money("0"),
        )
    assert snapshot.exposure.margin_available == Money.zero(Currency.INR)
    assert snapshot.exposure.margin_used == Money.of("1422307.25", Currency.INR)
    assert any("MARGIN_OVERDRAWN" in record.message for record in caplog.records)


def test_paper_session_tick_continues_with_margin_overdraft(tmp_path: Path) -> None:
    """Portfolio risk telemetry must not crash the live PAPER session loop."""
    clock = FrozenClock(NOW)
    ids = SequentialIdFactory(clock.instant)
    fill_model = load_evaluation_config(
        ROOT / "config" / "evaluation.yaml"
    ).config.fill_model
    broker = _overdrawn_broker(clock, ids, fill_model=fill_model)
    store_path = tmp_path / "paper.sqlite"
    from trading.storage.trading_store import TradingStore

    store = TradingStore.open(store_path, clock=clock)
    runner = PaperRunner(
        account_config=_paper_config(),  # type: ignore[arg-type]
        risk_policy=load_risk_policy(ROOT / "config" / "risk.yaml"),
        store=store,
        broker=broker,
        clock=clock,
        id_factory=ids,
        fill_model=fill_model,
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
        return ((), snapshots)

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
        risk_journal=PortfolioRiskJournal(tmp_path / "portfolio_risk"),
        risk_policy=load_risk_policy(ROOT / "config" / "risk.yaml").config,
        account_id=ACCOUNT_ID,
    )
    session.tick()
    assert session._cycle_count == 1
    journal_files = list((tmp_path / "portfolio_risk").glob("*.jsonl"))
    assert journal_files
    store.close()
