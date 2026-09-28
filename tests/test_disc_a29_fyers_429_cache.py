"""DISC-A29: paper session tolerates Fyers 429 and reuses cached status/chains."""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import UTC, datetime
from datetime import time as dt_time
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

import tests.factories as f
from tests.test_paper_runner import BROKER_FIXTURES, ROOT, _paper_config, _request
from trading.broker.paper import PaperBroker
from trading.config import load_evaluation_config, load_risk_policy
from trading.data.events import RawMarketCapture
from trading.data.fyers.client import FyersApiError
from trading.data.fyers.rate_limit import FyersRestRateLimiter
from trading.data.fyers.resilient_feed import ResilientFyersMarketFeed
from trading.domain.clock import FrozenClock
from trading.domain.contracts import DerivativesContext
from trading.domain.enums import OptionType
from trading.domain.ids import SequentialIdFactory
from trading.runtime.paper_runner import PaperRunner
from trading.runtime.paper_session import (
    PaperSession,
    _merge_following_week_chain,
    load_paper_session_config,
)
from trading.storage.trading_store import TradingStore

NOW = datetime(2026, 9, 28, 6, 28, tzinfo=UTC)
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


def _session(
    runner: PaperRunner,
    clock: FrozenClock,
    sink: _Sink,
    tmp_path: Path,
    builder: object,
) -> PaperSession:
    session_cfg = load_paper_session_config(ROOT / "config" / "paper_session.yaml")
    return PaperSession(
        runner=runner,
        clock=clock,
        session_config=session_cfg,
        session_hours=(dt_time(9, 15), dt_time(15, 30)),
        timezone=IST,
        notifier=sink,
        request_builder=builder,  # type: ignore[arg-type]
        observation_start=NOW,
        capital_limit=f.money("700000"),
        risk_policy_version="4",
        fill_model_version="conservative-v1",
        code_version="1",
        charges_verified=False,
        cohort_dir=tmp_path / "cohorts",
    )


def test_market_status_429_does_not_abort_paper_cycle(
    store: TradingStore, clock: FrozenClock, tmp_path: Path
) -> None:
    """Invariant 6: transient marketStatus 429 keeps the session loop alive."""
    runner = _runner(store, clock)
    sink = _Sink()
    request = _request()
    snapshots = {
        request.candidates[0].contract.symbol: request.candidates[0],
        request.underlying.contract.symbol: request.underlying,
    }
    calls = {"count": 0}

    def builder(_now: datetime) -> object:
        calls["count"] += 1
        if calls["count"] == 1:
            raise FyersApiError("Fyers error 429: request limit reached")
        return (request,), snapshots

    session = _session(runner, clock, sink, tmp_path, builder)
    assert session.tick() is None
    result = session.tick()
    assert result is not None
    assert calls["count"] == 2


def test_resilient_feed_reuses_cached_market_status_on_429(
    clock: FrozenClock,
) -> None:
    """Cached market status is served when a live fetch hits 429."""
    status_calls = 0
    cached_payload = {"s": "ok", "marketStatus": [{"status": "OPEN"}]}
    mono = {"now": 1000.0}

    class _Inner:
        def fetch_market_status(self) -> RawMarketCapture:
            nonlocal status_calls
            status_calls += 1
            if status_calls == 1:
                return RawMarketCapture(
                    capture_id="status-1",
                    provider="fyers",
                    endpoint="marketStatus",
                    received_at=clock.now_utc(),
                    payload=cached_payload,
                    http_status=200,
                )
            raise FyersApiError("Fyers error 429: request limit reached")

    limiter = FyersRestRateLimiter(clock)
    feed = ResilientFyersMarketFeed(_Inner(), limiter, clock)  # type: ignore[arg-type]

    with patch("trading.data.fyers.resilient_feed.time.monotonic", lambda: mono["now"]):
        first = feed.fetch_market_status()
        mono["now"] += 120.0
        second = feed.fetch_market_status()

    assert first.payload == cached_payload
    assert second.payload == cached_payload
    assert status_calls == 2


@patch("trading.runtime.paper_session.normalize_fyers_option_chain")
@patch("trading.runtime.paper_session.build_option_candidates")
def test_following_week_chain_cache_avoids_refetch_within_ttl(
    build_candidates: object,
    normalize_chain: object,
    tmp_path: Path,
) -> None:
    """TTL cache serves supplemental rows without a second Fyers REST call."""
    from trading.data.events import CanonicalMarketEvent
    from trading.data.storage.instrument_store import InstrumentSpecStore
    from trading.runtime.paper_session import _FollowingWeekChainCache

    fw_rows = (
        f.snapshot(
            contract=f.option_contract(
                symbol="NIFTY26OCT24000CE", strike=Decimal("24000")
            ),
            derivatives=DerivativesContext(
                days_to_expiry=29,
                open_interest=5000,
                option_type=OptionType.CALL,
                underlying_price=f.price("24000"),
            ),
        ),
    )
    build_candidates.return_value = (fw_rows, {})  # type: ignore[attr-defined]
    normalize_chain.return_value = CanonicalMarketEvent(  # type: ignore[attr-defined]
        event_id="evt-fw",
        event_type="OPTION_CHAIN_SNAPSHOT",
        symbol="NSE:NIFTY50-INDEX",
        event_time=datetime(2026, 9, 28, 4, 0, tzinfo=UTC),
        receive_time=datetime(2026, 9, 28, 4, 0, tzinfo=UTC),
        source_time=datetime(2026, 9, 28, 4, 0, tzinfo=UTC),
        provider="fyers",
        provider_sequence=1,
        normalization_version="1",
        raw_ref="test",
        payload={
            "expiry_data": [
                {"date": "29-09-2026", "expiry": "1790676600"},
                {"date": "06-10-2026", "expiry": "1791281400"},
            ],
            "strikes": [],
        },
    )
    fetch_calls = 0

    class _Feed:
        def fetch_option_chain(
            self, symbol: str, *, expiry_epoch: int | None = None
        ) -> object:
            nonlocal fetch_calls
            fetch_calls += 1
            return object()

    chain = normalize_chain.return_value  # type: ignore[attr-defined]
    near = f.snapshot(
        contract=f.option_contract(symbol="NIFTY26SEP24000CE", strike=Decimal("24000")),
        derivatives=DerivativesContext(
            days_to_expiry=1,
            open_interest=5000,
            option_type=OptionType.CALL,
            underlying_price=f.price("24000"),
        ),
    )
    cache = _FollowingWeekChainCache(
        fetched_at=time.monotonic(),
        epoch=1791281400,
        candidates=fw_rows,
        specs={},
    )
    merged, specs, error, returned_cache = _merge_following_week_chain(
        (near,),
        {},
        chain=chain,
        catalog=InstrumentSpecStore(tmp_path / "instruments"),
        underlying=f.snapshot(contract=f.index_contract()),
        as_of=datetime(2026, 9, 28, 4, 0, tzinfo=UTC),
        zone=IST,
        feed=_Feed(),  # type: ignore[arg-type]
        pipeline_symbol="NSE:NIFTY50-INDEX",
        cache_ttl_seconds=300,
        cache=cache,
    )
    assert fetch_calls == 0
    assert error is None
    assert returned_cache is cache
    assert len(merged) == 2
    assert specs == {}
