"""End-to-end M2 agentic pipeline with mock Fyers REST and simulated tick store."""

from __future__ import annotations

import json
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import polars as pl

from trading.ai.m2_agentic_agent.cycle import run_m2_agentic_agent_cycle
from trading.analytics.m2_agentic_telemetry import decisions_log_path
from trading.data.m2_agentic_pipeline.orchestrate import run_minute_and_features
from trading.data.tick_consumer.models import Bar1m, SignedTick, TickSign
from trading.data.tick_consumer.store import TickStore, bars_parquet_path
from trading.domain.clock import FrozenClock
from trading.trade.m2_veto import load_m2_veto_config
from tests.fixtures.mock_fyers_market_feed import MockFyersMarketFeed

_REPO = Path(__file__).resolve().parents[1]
_IST = ZoneInfo("Asia/Kolkata")


def _seed_repo(tmp_path: Path) -> tuple[Path, FrozenClock]:
    (tmp_path / "config").mkdir(parents=True)
    for name in ("m2_agentic_pipeline.yaml", "data_pipeline.yaml", "m2_veto.yaml"):
        shutil.copy(_REPO / "config" / name, tmp_path / "config" / name)
    veto_yaml = tmp_path / "config" / "m2_veto.yaml"
    text = veto_yaml.read_text(encoding="utf-8")
    text = text.replace("data/paper/", "data/test_paper/")
    veto_yaml.write_text(text, encoding="utf-8")
    (tmp_path / "data" / "test_paper").mkdir(parents=True, exist_ok=True)
    mark = datetime(2026, 10, 31, 9, 20, tzinfo=_IST)
    clock = FrozenClock(mark.astimezone(UTC))
    return tmp_path, clock


def _seed_tick_ingestion(repo_root: Path, clock: FrozenClock) -> None:
    """Simulate tick daemon output: bars parquet + delta-friendly volume."""
    session = clock.now_utc().astimezone(_IST).date()
    store = TickStore(repo_root)
    base_ltp = 24500.0
    for index in range(3):
        ts = clock.now_utc()
        signed = SignedTick(
            symbol="NSE:NIFTY50-INDEX",
            ltp=f"{base_ltp + index}",
            volume=1000 + index * 10,
            trade_size=50,
            exchange_ts=ts,
            received_at=ts,
            sign=TickSign.BUY,
        )
        store.append_tick(signed, session_date=session)
        bar = Bar1m(
            symbol="NSE:NIFTY50-INDEX",
            bar_start_ist=ts.astimezone(_IST).replace(second=0, microsecond=0),
            open=str(base_ltp),
            high=str(base_ltp + 5),
            low=str(base_ltp - 5),
            close=str(base_ltp + index),
            volume=500 + index * 20,
            buy_volume=300,
            sell_volume=200,
            unknown_volume=0,
            tick_count=10,
        )
        store.append_bar(bar, session_date=session)
    store.flush_ticks(session_date=session)
    store.flush_bars(session_date=session)
    bar_path = bars_parquet_path(repo_root, session)
    assert bar_path.is_file()
    frame = pl.read_parquet(bar_path)
    assert frame.height >= 1


def test_mock_fyers_pipeline_stages_produce_linked_artifacts(tmp_path: Path) -> None:
    repo_root, clock = _seed_repo(tmp_path)
    mock = MockFyersMarketFeed(clock)
    _seed_tick_ingestion(repo_root, clock)

    t0 = time.perf_counter()
    minute_features = run_minute_and_features(repo_root, clock=clock, market_feed=mock)
    minute_elapsed = time.perf_counter() - t0
    assert minute_features is not None
    minute_path, feature_path = minute_features
    assert minute_path.is_file()
    assert feature_path.is_file()

    bundle = json.loads(minute_path.read_text(encoding="utf-8"))
    features = json.loads(feature_path.read_text(encoding="utf-8"))
    assert bundle["nifty_quote"]["ltp"] == "24500.00"
    assert features.get("session_date") == bundle.get("session_date")

    t1 = time.perf_counter()
    decision_path = run_m2_agentic_agent_cycle(
        repo_root,
        clock=clock,
        llm_enabled=False,
    )
    agent_elapsed = time.perf_counter() - t1
    assert decision_path is not None and decision_path.is_file()

    packet = json.loads(decision_path.read_text(encoding="utf-8"))
    assert packet["m2_agentic_id"]
    assert packet["lineage"]["feature_row_path"] == str(feature_path.relative_to(repo_root))
    assert packet["fail_closed"] is False

    veto_cfg = load_m2_veto_config(repo_root / "config" / "m2_veto.yaml")
    from trading.trade.m2_veto import verdict_file

    verdict = json.loads(verdict_file(veto_cfg, repo_root=repo_root).read_text(encoding="utf-8"))
    assert verdict["action"] in {"APPROVE", "VETO", "SIZE_DOWN"}
    assert verdict.get("m2_agentic_id") == packet["m2_agentic_id"]

    session_date = str(packet["session_date"])
    log_path = decisions_log_path(repo_root, session_date)
    assert log_path.is_file()
    lines = [line for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["m2_agentic_id"] == packet["m2_agentic_id"]
    assert record["m2_agentic_snapshot_path"] == str(decision_path.relative_to(repo_root))

    assert mock.rest_call_count == 3
    assert minute_elapsed < 5.0
    assert agent_elapsed < 5.0
