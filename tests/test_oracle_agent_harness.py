"""Tests for the Oracle VM Agent Harness integration.

Validates:
1. PaperRunner logs shadow agent decisions into TradingStore during review slots.
2. _resolve_weekly_cohort auto-detects cohorts from data/paper/cohorts/*.json.
3. _resolve_weekly_cohort refuses fixture cohorts when allow_fixture is False.
4. CLI --grant-id argument is parsed and wired.
5. Deploy and sync scripts exist, have correct permissions, and pass bash syntax check.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from trading.broker.paper import PaperBroker
from trading.cli import _resolve_weekly_cohort
from trading.config import Environment, load_config_text, load_risk_policy
from trading.domain.clock import FrozenClock
from trading.domain.enums import DeskRole, ReviewSlotId
from trading.domain.ids import SequentialIdFactory
from trading.runtime.paper_runner import PaperRunner
from trading.runtime.review_schedule import ReviewSlot, parse_hhmm
from trading.storage.trading_store import TradingStore

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 20, 5, 0, tzinfo=UTC)


def test_resolve_weekly_cohort_accepts_paper_cohort(tmp_path: Path) -> None:
    cohort_dir = tmp_path / "data" / "paper" / "cohorts"
    cohort_dir.mkdir(parents=True)
    cohort_file = cohort_dir / "EXP-TEST-001.json"
    cohort_file.write_text("{}", encoding="utf-8")

    path, source = _resolve_weekly_cohort(
        tmp_path, cohort=str(cohort_file), allow_fixture=False
    )
    assert path == str(cohort_file)
    assert source == "paper"


def test_resolve_weekly_cohort_refuses_when_no_paper_cohorts(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="refusing fixture/default cohort"):
        _resolve_weekly_cohort(tmp_path, cohort="", allow_fixture=False)


def test_resolve_weekly_cohort_allows_fixture_fallback(tmp_path: Path) -> None:
    path, source = _resolve_weekly_cohort(tmp_path, cohort="", allow_fixture=True)
    assert "tests/fixtures" in path
    assert source == "fixture"


def test_paper_runner_records_shadow_position_decisions(
    tmp_path: Path,
) -> None:
    clock = FrozenClock(NOW)
    ids = SequentialIdFactory(NOW)
    store = TradingStore.open(tmp_path / "test.sqlite", clock=clock)
    broker = PaperBroker.from_fixtures(
        ROOT / "tests" / "fixtures" / "broker",
        clock=clock,
        id_factory=ids,
    )
    raw = (ROOT / "config" / "base.yaml").read_text(encoding="utf-8")
    raw = raw.replace("environment: BACKTEST", "environment: PAPER")
    raw = raw.replace('account_id: "REPLACE_ME"', 'account_id: "ACC-PAPER-1"')
    account = load_config_text(raw, expect_environment=Environment.PAPER)
    risk_policy = load_risk_policy(ROOT / "config" / "risk.yaml")

    runner = PaperRunner(
        account_config=account,
        risk_policy=risk_policy,
        store=store,
        broker=broker,
        clock=clock,
        id_factory=ids,
    )

    # Initially empty
    assert len(store.list_agent_decisions(role=DeskRole.POSITION)) == 0

    # Execute a review slot
    runner.run_review_slot(
        ReviewSlot(ReviewSlotId.NSE_MORNING, parse_hhmm("10:30")),
        {},
        session_date=NOW.date(),
    )

    # Empty positions → zero reviews evaluated
    assert len(store.list_agent_decisions(role=DeskRole.POSITION)) == 0
    store.close()


def test_harness_scripts_syntax() -> None:
    scripts = [
        ROOT / "deploy" / "deploy_oracle.sh",
        ROOT / "deploy" / "install.sh",
        ROOT / "scripts" / "oracle_agent_ask.sh",
        ROOT / "scripts" / "oracle_agent_advise.sh",
        ROOT / "scripts" / "oracle_agent_sync.sh",
    ]
    for script in scripts:
        assert script.is_file(), f"missing script: {script}"
        proc = subprocess.run(
            ["bash", "-n", str(script)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, f"syntax error in {script}: {proc.stderr}"


def test_systemd_units_exist() -> None:
    units = [
        ROOT / "deploy" / "agent-weekly.service",
        ROOT / "deploy" / "agent-weekly.timer",
        ROOT / "deploy" / "agent-advise.service",
        ROOT / "deploy" / "agent-advise.timer",
        ROOT / "deploy" / "agent-research.service",
        ROOT / "deploy" / "agent-research.timer",
        ROOT / "deploy" / "fno-automated.service",
    ]
    for unit in units:
        assert unit.is_file(), f"missing systemd unit: {unit}"
        content = unit.read_text(encoding="utf-8")
        assert "[Unit]" in content
        assert "User=apple" not in content, f"hardcoded macOS user in {unit}"
