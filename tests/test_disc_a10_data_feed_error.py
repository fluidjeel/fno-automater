"""DISC-A10: PAPER session survives data/feed builder errors without crashing."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import tests.factories as f
from tests.test_paper_runner import BROKER_FIXTURES, ROOT, _paper_config, _request
from trading.broker.paper import PaperBroker
from trading.config import load_evaluation_config, load_risk_policy
from trading.data.fyers.client import FyersApiError
from trading.domain.clock import FrozenClock
from trading.domain.contracts.discovery_decision import DiscoveryDecision
from trading.domain.contracts import DerivativesContext
from trading.domain.enums import (
    DiscoveryDecisionKind,
    DiscoveryStage,
    FamilyId,
    ModeId,
    OptionType,
    ReasonCode,
    Side,
)
from trading.domain.ids import SequentialIdFactory
from trading.domain.primitives import Currency, Money
from trading.runtime.paper_runner import PaperRunner
from trading.runtime.paper_session import PaperSession, load_paper_session_config
from trading.storage.trading_store import TradingEventType, TradingStore

NOW = datetime(2026, 9, 14, 4, 0, tzinfo=UTC)
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


def _feed_error_rows(store: TradingStore) -> list[DiscoveryDecision]:
    rows: list[DiscoveryDecision] = []
    for event in store.read_events():
        if event.event_type is not TradingEventType.DISCOVERY_DECISION:
            continue
        payload = event.deserialize()
        if (
            isinstance(payload, DiscoveryDecision)
            and ReasonCode.DATA_FEED_ERROR in payload.reason_codes
            and payload.family_id == "DATA_FEED"
        ):
            rows.append(payload)
    return rows


def test_builder_401_on_cycles_one_two_then_succeeds(
    store: TradingStore, clock: FrozenClock, tmp_path: Path
) -> None:
    """Invariant 6: feed errors block entries but the session loop keeps running."""
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
        if calls["count"] <= 2:
            raise FyersApiError("Fyers error 401: token expired")
        return (request,), snapshots

    session = _session(runner, clock, sink, tmp_path, builder)
    assert session.tick() is None
    assert session.tick() is None
    result = session.tick()
    assert result is not None
    assert calls["count"] == 3
    feed_rows = _feed_error_rows(store)
    assert len(feed_rows) == 2
    for row in feed_rows:
        assert row.stage is DiscoveryStage.DATA
        assert row.decision is DiscoveryDecisionKind.BLOCKED_HARD
        assert "401" in row.reason_text or "token" in row.reason_text.lower()


def test_open_position_exits_use_protection_quotes_when_builder_fails(
    store: TradingStore,
    tmp_path: Path,
) -> None:
    """Invariant 8: protection quotes still drive exits when the builder fails."""
    clock = FrozenClock(NOW_CTX)
    runner = _runner(store, clock)
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
    sells_before = sum(
        1 for event in runner.broker.list_orders() if event.command.side is Side.SELL
    )

    sink = _Sink()

    def builder(_now: datetime) -> object:
        raise FyersApiError("Fyers error 500: upstream unavailable")

    session = _session(runner, clock, sink, tmp_path, builder)
    assert session.tick() is None
    sells_after = sum(
        1 for event in runner.broker.list_orders() if event.command.side is Side.SELL
    )
    assert sells_after > sells_before


def test_three_failures_send_one_alert_and_recovery_sends_one(
    store: TradingStore, clock: FrozenClock, tmp_path: Path
) -> None:
    """After three builder failures exactly one alert; recovery sends one more."""
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
        if calls["count"] <= 3:
            raise FyersApiError("Fyers error 401: token expired")
        return (request,), snapshots

    session = _session(runner, clock, sink, tmp_path, builder)
    for _ in range(3):
        assert session.tick() is None
    failure_alerts = [
        msg
        for msg in sink.messages
        if "token expired" in msg or "consecutive" in msg.lower()
    ]
    assert len(failure_alerts) == 1
    assert "Fyers token expired — refresh token" in failure_alerts[0]

    assert session.tick() is not None
    recovery_alerts = [msg for msg in sink.messages if "recovered" in msg.lower()]
    assert len(recovery_alerts) == 1


def test_following_week_fetch_failure_records_reason_and_keeps_near_chain(
    tmp_path: Path,
) -> None:
    """Partial following-week failure records detail but keeps the near chain."""
    from trading.data.events import CanonicalMarketEvent
    from trading.data.storage.instrument_store import InstrumentSpecStore
    from trading.runtime.paper_session import _merge_following_week_chain

    chain = CanonicalMarketEvent(
        event_id="evt-1",
        event_type="OPTION_CHAIN_SNAPSHOT",
        symbol="NSE:NIFTY50-INDEX",
        event_time=NOW,
        receive_time=NOW,
        source_time=NOW,
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
    underlying = f.snapshot(contract=f.index_contract())
    near = f.snapshot(
        contract=f.option_contract(
            symbol="NIFTY26SEP24000CE", strike=Decimal("24000")
        ),
        derivatives=DerivativesContext(
            days_to_expiry=10,
            open_interest=5000,
            option_type=OptionType.CALL,
            underlying_price=f.price("24000"),
        ),
    )

    class _FailingFeed:
        def fetch_option_chain(
            self, symbol: str, *, expiry_epoch: int | None = None
        ) -> object:
            raise FyersApiError("Fyers error 401: token expired")

    merged, specs, error = _merge_following_week_chain(
        (near,),
        {},
        chain=chain,
        catalog=InstrumentSpecStore(tmp_path / "instruments"),
        underlying=underlying,
        as_of=datetime(2026, 9, 25, 4, 0, tzinfo=UTC),
        zone=IST,
        feed=_FailingFeed(),  # type: ignore[arg-type]
        pipeline_symbol="NSE:NIFTY50-INDEX",
    )
    assert merged == (near,)
    assert error is not None
    assert "following-week" in error
    assert "401" in error
    assert specs == {}
