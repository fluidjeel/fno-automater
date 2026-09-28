"""DISC-A34: rate limiter waits; pipeline and session survive local 429."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from datetime import time as dt_time
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

import tests.factories as f
from tests.test_data_pipeline import (
    BASE_CONFIG,
    PIPELINE_CONFIG,
    FakeFeed,
    _capture,
    _history_capture,
    _underlying,
)
from tests.test_paper_runner import BROKER_FIXTURES, ROOT, _paper_config, _request
from trading.broker.paper import PaperBroker
from trading.config import load_evaluation_config, load_risk_policy
from trading.data.events import RawMarketCapture
from trading.data.fyers.client import FyersApiError
from trading.data.fyers.rate_limit import FyersRestRateLimiter
from trading.data.fyers.resilient_feed import ResilientFyersMarketFeed
from trading.data.normalize import normalize_fyers_history
from trading.data.pipeline import DataPipeline
from trading.data.storage.parquet_store import JsonlEventStore
from trading.domain.clock import FrozenClock
from trading.domain.enums import FamilyId, ModeId
from trading.runtime.paper_runner import PaperRunner
from trading.runtime.paper_session import PaperSession, load_paper_session_config
from trading.runtime.protection import build_protection_coordinator
from trading.runtime.rest_quote_monitor import RestQuoteMonitor
from trading.storage.trading_store import TradingStore

NOW = datetime(2026, 9, 28, 6, 28, tzinfo=UTC)
NOW_CTX = NOW + timedelta(seconds=60)
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


def test_rate_limiter_waits_instead_of_immediately_failing() -> None:
    """Local REST throttle waits for a token before giving up."""
    clock = FrozenClock(NOW)
    limiter = FyersRestRateLimiter(clock, max_per_second=1)
    assert limiter.acquire() is True

    sleeps: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock.advance(timedelta(seconds=max(seconds, 1.01)))

    assert (
        limiter.acquire_or_wait(
            max_wait=timedelta(seconds=2),
            sleep=fake_sleep,
        )
        is True
    )
    assert sleeps
    assert limiter.acquire() is False


def test_resilient_feed_waits_for_history_before_raising() -> None:
    """History fetch blocks on the limiter instead of raising immediately."""
    clock = FrozenClock(NOW)
    limiter = FyersRestRateLimiter(clock, max_per_second=1)
    history_calls = 0
    payload = {"s": "ok", "candles": [[int(NOW.timestamp()), 1, 2, 0.5, 1, 100]]}

    class _Inner:
        def fetch_history(
            self,
            symbol: str,
            *,
            resolution: str,
            range_from: str,
            range_to: str,
            date_format: int = 1,
            cont_flag: int = 0,
            oi_flag: int | None = None,
        ) -> RawMarketCapture:
            nonlocal history_calls
            history_calls += 1
            return RawMarketCapture(
                capture_id=f"hist-{history_calls}",
                provider="fyers",
                endpoint="history",
                received_at=clock.now_utc(),
                payload=payload,
                http_status=200,
            )

    feed = ResilientFyersMarketFeed(_Inner(), limiter, clock)  # type: ignore[arg-type]
    assert limiter.acquire() is True

    with patch(
        "trading.data.fyers.rate_limit.time.sleep",
        side_effect=lambda seconds: clock.advance(
            timedelta(seconds=max(seconds, 1.01))
        ),
    ):
        first = feed.fetch_history(
            "NSE:NIFTY50-INDEX",
            resolution="5",
            range_from="2026-09-01",
            range_to="2026-09-28",
        )
        second = feed.fetch_history(
            "NSE:NIFTY50-INDEX",
            resolution="5",
            range_from="2026-09-01",
            range_to="2026-09-28",
        )

    assert history_calls == 1
    assert first.payload == payload
    assert second.payload == payload


def test_run_once_survives_history_429_with_stored_bars(tmp_path: Path) -> None:
    """Pipeline degrades on 429 and reuses persisted BAR_SNAPSHOT events."""
    now = datetime(2024, 9, 13, 6, 0, tzinfo=UTC)
    store = JsonlEventStore(tmp_path / "data")
    history_capture = _history_capture(now)
    bar_event = normalize_fyers_history(
        history_capture,
        symbol="NSE:NIFTY50-INDEX",
        resolution="5",
        normalization_version="1",
        raw_ref="seed.json",
    )
    store.append_canonical(bar_event)

    class _History429Feed(FakeFeed):
        def fetch_history(
            self,
            symbol: str,
            *,
            resolution: str,
            range_from: str,
            range_to: str,
        ) -> RawMarketCapture:
            raise FyersApiError("Fyers error 429: request limit reached")

    pipeline_config = __import__(
        "trading.data.config", fromlist=["load_data_pipeline_config"]
    ).load_data_pipeline_config(PIPELINE_CONFIG)
    pipeline = DataPipeline(
        pipeline_config=pipeline_config,
        feed=_History429Feed(_capture(now)),
        store=store,
        app_config_path=BASE_CONFIG,
        repo_root=tmp_path,
        clock=FrozenClock(now),
    )
    result = pipeline.run_once(_underlying(), now=now)
    assert result.snapshot is not None
    assert any(event.event_type == "BAR_SNAPSHOT" for event in result.events)


def _runner(store: TradingStore, clock: FrozenClock) -> PaperRunner:
    ids = __import__(
        "trading.domain.ids", fromlist=["SequentialIdFactory"]
    ).SequentialIdFactory(clock.instant)
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


def test_paper_cycle_survives_429_and_protection_still_ticks(
    tmp_path: Path,
) -> None:
    """Invariant 8: 429 in the builder skips entries but protection keeps ticking."""
    session_now = datetime(2026, 9, 14, 4, 0, tzinfo=UTC) + timedelta(seconds=60)
    clock = FrozenClock(session_now)
    trading_store = TradingStore.open(tmp_path / "paper.sqlite", clock=clock)
    runner = _runner(trading_store, clock)
    request = _request(
        forced_mode_id=ModeId.M2_DIRECTIONAL,
        forced_family_id=FamilyId.long_call,
    )
    result = runner.run_cycle((request,))
    assert result.outcomes[0].order_events
    option = request.candidates[0]
    stop_snap = option.model_copy(
        update={
            "market": option.market.model_copy(
                update={
                    "last": f.price("85.00"),
                    "bid": f.price("85.00"),
                    "ask": f.price("85.20"),
                }
            )
        }
    )
    runner.seed_protection_snapshots({option.contract.symbol: stop_snap})
    protection_ticks = {"count": 0}
    calls = {"count": 0}

    def builder(_now: datetime) -> object:
        calls["count"] += 1
        raise FyersApiError("Fyers error 429: request limit reached")

    session_cfg = load_paper_session_config(ROOT / "config" / "paper_session.yaml")

    def _rest_fetch(symbols: tuple[str, ...]) -> dict[str, object]:
        return {
            symbol: runner.protection_snapshots[symbol].market
            for symbol in symbols
            if symbol in runner.protection_snapshots
        }

    rest_monitor = RestQuoteMonitor(
        clock,
        _rest_fetch,  # type: ignore[arg-type]
        poll_seconds=1,
    )
    protection = build_protection_coordinator(
        runner=runner,
        clock=clock,
        config=session_cfg.protection,
        repo_root=tmp_path,
        rest_fetch=rest_monitor,
        ws=None,
    )
    original_tick = protection.tick

    def counting_tick(**_kwargs: object) -> None:
        protection_ticks["count"] += 1
        original_tick(**_kwargs)  # type: ignore[arg-type]

    protection.tick = counting_tick  # type: ignore[method-assign]
    session = PaperSession(
        runner=runner,
        clock=clock,
        session_config=session_cfg,
        session_hours=(dt_time(9, 15), dt_time(15, 30)),
        timezone=IST,
        notifier=_Sink(),
        request_builder=builder,  # type: ignore[arg-type]
        observation_start=NOW,
        capital_limit=f.money("700000"),
        risk_policy_version="4",
        fill_model_version="conservative-v1",
        code_version="1",
        charges_verified=False,
        cohort_dir=tmp_path / "cohorts",
        protection=protection,
        sleeper=lambda _seconds: None,
    )
    session.run(once=True)
    trading_store.close()
    assert calls["count"] == 1
    assert protection_ticks["count"] == 1
