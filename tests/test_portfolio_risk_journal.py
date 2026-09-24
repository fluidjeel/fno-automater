"""Portfolio risk journal: durable Greek surface and stress snapshots."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import tests.factories as f
from tests.test_dashboard import _make_repo
from trading.broker.paper import PaperBroker
from trading.config import load_risk_policy
from trading.dashboard import collector
from trading.domain.clock import FrozenClock
from trading.domain.contracts.snapshot import DerivativesContext, Greeks
from trading.domain.enums import RiskAction, Side, TradeState
from trading.domain.ids import SequentialIdFactory
from trading.portfolio.risk_journal import (
    PortfolioRiskJournal,
    build_portfolio_risk_record,
)

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 20, 5, 0, tzinfo=UTC)
POLICY = load_risk_policy(ROOT / "config" / "risk.yaml").config


def test_journal_appends_and_reads_latest(tmp_path: Path) -> None:
    clock = FrozenClock(NOW)
    ids = SequentialIdFactory(clock.instant)
    broker = PaperBroker.from_fixtures(
        ROOT / "tests" / "fixtures" / "broker",
        clock=clock,
        id_factory=ids,
    )
    journal = PortfolioRiskJournal(tmp_path / "portfolio_risk")
    record = build_portfolio_risk_record(
        broker=broker,
        account_id="ACC-PAPER-1",
        positions=(),
        open_book={},
        snapshots={},
        risk_policy=POLICY,
        id_factory=ids,
        as_of=NOW,
    )
    journal.append(record)
    latest = journal.read_latest()
    assert latest is not None
    assert latest.portfolio_snapshot_id == record.portfolio_snapshot_id
    assert latest.exposure["net_delta"] == "0"


def test_build_record_aggregates_open_position_greeks(tmp_path: Path) -> None:
    clock = FrozenClock(NOW)
    ids = SequentialIdFactory(clock.instant)
    broker = PaperBroker.from_fixtures(
        ROOT / "tests" / "fixtures" / "broker",
        clock=clock,
        id_factory=ids,
    )
    contract = f.option_contract()
    snapshot = f.snapshot(
        contract=contract,
        derivatives=DerivativesContext(
            days_to_expiry=5,
            greeks=Greeks(
                model="bs",
                calculation_version="1",
                converged=True,
                delta=Decimal("0.5"),
                vega=Decimal("10"),
            ),
        ),
        features={"lot_size": Decimal("25")},
    )
    intent = f.intent(estimated_max_loss=f.money("25000"))
    decision = f.risk_decision(
        recalculated_max_loss=f.money("25000"),
        action=RiskAction.APPROVE,
    )
    position = f.position_state(
        trade_id="TRD-1",
        state=TradeState.OPEN,
        legs=(
            f.position_leg_state(
                contract=contract,
                side=Side.BUY,
                quantity_contracts=1,
            ),
        ),
    )
    record = build_portfolio_risk_record(
        broker=broker,
        account_id="ACC-PAPER-1",
        positions=(position,),
        open_book={"TRD-1": (intent, decision)},
        snapshots={snapshot.contract.symbol: snapshot},
        risk_policy=POLICY,
        id_factory=ids,
        as_of=NOW,
    )
    assert record.open_position_count == 1
    assert Decimal(str(record.exposure["net_delta"])) == Decimal("12.5")
    assert Decimal(str(record.exposure["net_vega"])) == Decimal("250")
    assert record.stress["breached_budget"] is False


def test_dashboard_reads_portfolio_risk_journal(
    tmp_path: Path, monkeypatch: Any
) -> None:
    _make_repo(tmp_path)
    clock = FrozenClock(NOW)
    ids = SequentialIdFactory(clock.instant)
    broker = PaperBroker.from_fixtures(
        ROOT / "tests" / "fixtures" / "broker",
        clock=clock,
        id_factory=ids,
    )
    journal_dir = tmp_path / "data" / "paper" / "portfolio_risk"
    journal = PortfolioRiskJournal(journal_dir)
    journal.append(
        build_portfolio_risk_record(
            broker=broker,
            account_id="ACC-PAPER-1",
            positions=(),
            open_book={},
            snapshots={},
            risk_policy=POLICY,
            id_factory=ids,
            as_of=NOW,
        )
    )
    monkeypatch.setattr(collector, "_systemd", lambda _root: [])
    monkeypatch.setattr(collector, "_run", lambda *_args, **_kwargs: "testrev")
    monkeypatch.setattr(
        collector,
        "_host",
        lambda _root: {"hostname": "test", "load_average": [0, 0, 0]},
    )
    snapshot = collector.build_dashboard_snapshot(tmp_path, source="test")
    assert snapshot["portfolio"]["risk"]["state"] == "OBSERVED"
    assert snapshot["portfolio"]["risk"]["latest_at"] is not None
    assert not any(
        finding["title"]
        == "Portfolio Greeks and scenario P&L are not durably journaled"
        for finding in snapshot["findings"]
    )
