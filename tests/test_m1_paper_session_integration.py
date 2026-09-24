"""M1 CAS PaperSession integration: event path, not the 60-second poll."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import tests.factories as f
from tests.test_four_mode_session_integration import MODES, _runner, _spec
from tests.test_p7_m1_selector_and_g3_path import (
    CAS_NOW,
    _macro,
    _market,
    _option,
    _underlying,
)
from tests.test_paper_lifecycle import _restart
from trading.domain.clock import FrozenClock
from trading.domain.contracts import FeatureSnapshot, InstrumentSpec
from trading.domain.enums import (
    ExecutionMode,
    ModeId,
    OptionType,
    OrderState,
    TradeState,
)
from trading.domain.primitives import Currency, Money
from trading.identification import load_identification_policy
from trading.runtime.cas_event_path import (
    SIMULATED,
    CasEventDrivenConfig,
    M1ProviderEvent,
    active_m1_window,
    evaluate_m1_provider_event,
    measure_cas_entry_latency,
    record_episode_attempt,
)
from trading.runtime.four_mode_producers import (
    build_four_mode_requests,
    produce_family_requests,
)
from trading.runtime.paper_runner import PaperStrategyRequest
from trading.runtime.paper_session import (
    CycleBuild,
    PaperSession,
    PaperSessionConfig,
    load_paper_session_config,
)
from trading.runtime.startup_validation import validate_startup_configuration
from trading.storage.trading_store import TradingStore
from trading.strategies.macro import MacroBias

ROOT = Path(__file__).resolve().parent.parent

IST = ZoneInfo("Asia/Kolkata")
POLICY = load_identification_policy(ROOT / "config" / "identification.yaml")
NOW = CAS_NOW


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(NOW)


@pytest.fixture
def store(tmp_path: Path, clock: FrozenClock) -> Iterator[TradingStore]:
    trading_store = TradingStore.open(tmp_path / "m1_session.sqlite", clock=clock)
    yield trading_store
    trading_store.close()


class _Sink:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def send(self, text: str) -> bool:
        self.messages.append(text)
        return True


def _m1_chain() -> tuple[FeatureSnapshot, FeatureSnapshot, dict[str, InstrumentSpec]]:
    underlying = _underlying()
    call = _option(
        "NIFTY26SEP23700CE",
        strike="23700",
        delta="0.30",
        option_type=OptionType.CALL,
        ask="30.00",
        bid="29.95",
    ).model_copy(
        update={
            "market": f.quote(
                bid=f.price("29.95"),
                ask=f.price("30.00"),
                last=f.price("30.00"),
                bid_size=500,
                ask_size=500,
            )
        }
    )
    cheap_otm = _option(
        "NIFTY26SEP23500CE",
        strike="23500",
        delta="0.12",
        option_type=OptionType.CALL,
        ask="8.00",
        bid="7.95",
    )
    instruments = {
        call.contract.symbol: _spec(call.contract.symbol, "23700", OptionType.CALL),
        cheap_otm.contract.symbol: _spec(
            cheap_otm.contract.symbol, "23500", OptionType.CALL
        ),
    }
    return underlying, call, instruments


def _m1_fixture_builder(
    *,
    repo_root: Path,
    session_cfg: PaperSessionConfig,
    option_candidates: tuple[FeatureSnapshot, ...],
    underlying: FeatureSnapshot,
    instruments: dict[str, InstrumentSpec],
) -> CycleBuild:
    m1_holder: dict[str, object] = {"event": None, "keep": False}
    cas_attempts_today = 0
    cas_session_date: date | None = None
    cas_latency_samples: list[int] = []

    def build(
        now: datetime,
    ) -> tuple[tuple[PaperStrategyRequest, ...], dict[str, FeatureSnapshot]]:
        nonlocal cas_attempts_today, cas_session_date
        session_day = now.astimezone(IST).date()
        if cas_session_date != session_day:
            cas_session_date = session_day
            cas_attempts_today = 0
        macro = _macro(MacroBias.BULLISH)
        market = _market()
        event_risk = f.event_risk_state(
            as_of=now - timedelta(minutes=1),
            expires_at=now + timedelta(hours=1),
        )
        snapshots = {
            underlying.contract.symbol: underlying,
            **{item.contract.symbol: item for item in option_candidates},
        }
        produced = produce_family_requests(
            modes_config=MODES,
            mode_stances=session_cfg.mode_stances,
            family_stances=session_cfg.family_stances,
            candidates=option_candidates,
            market=market,
            policy=POLICY,
            p1=None,
            master_symbols=frozenset(instruments),
        )
        event = m1_holder.get("event")
        window = active_m1_window(now, session_cfg.cas_event_driven)
        in_window = window is not None
        latency_report = measure_cas_entry_latency(
            tuple(cas_latency_samples),
            config=session_cfg.cas_event_driven,
        )
        adjusted = []
        for item in produced:
            if item.spec.mode_id is not ModeId.M1_CAS:
                adjusted.append(item)
                continue
            m1_stance = session_cfg.mode_stances.get(
                ModeId.M1_CAS.value, ExecutionMode.SHADOW
            )
            gate = evaluate_m1_provider_event(
                item=item,
                event=event if isinstance(event, M1ProviderEvent) else None,
                option_candidates=option_candidates,
                config=session_cfg.cas_event_driven,
                stance=m1_stance,
                attempts_today=cas_attempts_today,
                in_window=in_window,
                latency_report=latency_report,
                session_date=session_day.isoformat(),
                episode_ledger=repo_root / "data" / "paper" / "m1_episodes.json",
                mode_capital=MODES.modes[ModeId.M1_CAS].capital_share
                * Decimal("700000"),
            )
            execute = gate.execute
            mode = gate.execution_mode
            if execute and isinstance(event, M1ProviderEvent):
                cas_attempts_today += 1
                record_episode_attempt(
                    repo_root / "data" / "paper" / "m1_episodes.json",
                    session_date=session_day.isoformat(),
                    episode_id=event.episode_id or item.spec.family_id.value,
                )
            adjusted.append(
                item.__class__(
                    spec=item.spec,
                    bound=item.bound,
                    execute=execute,
                    execution_mode=mode if execute else ExecutionMode.SHADOW,
                )
            )
        requests = build_four_mode_requests(
            adjusted,
            index_underlying=underlying,
            instruments=instruments,
            event_risk=event_risk,
            macro=macro,
            experiment_prefix=session_cfg.experiment_prefix,
            now=now,
        )
        return requests, snapshots

    build.m1_holder = m1_holder  # type: ignore[attr-defined]
    return build


def _session(
    store: TradingStore,
    clock: FrozenClock,
    tmp_path: Path,
    *,
    repo_root: Path,
    session_cfg: PaperSessionConfig,
    builder: CycleBuild,
) -> PaperSession:
    runner = _runner(store, clock)
    return PaperSession(
        runner=runner,
        clock=clock,
        session_config=session_cfg,
        session_hours=(time(9, 15), time(15, 30)),
        timezone=IST,
        notifier=_Sink(),
        request_builder=builder,
        observation_start=NOW,
        capital_limit=Money.of("700000", Currency.INR),
        risk_policy_version="4",
        fill_model_version="conservative-v1",
        code_version="m1-session-test",
        charges_verified=False,
        cohort_dir=tmp_path / "cohorts",
    )


def _simulated_event(
    *,
    now: datetime,
    episode_id: str = "ep-1",
    disconnected: bool = False,
    exchange_timestamp_observed: bool = True,
    allow_simulated_fixture: bool = True,
) -> M1ProviderEvent:
    return M1ProviderEvent(
        receive_time=now,
        decided_at=now + timedelta(milliseconds=50),
        provenance=SIMULATED,
        profile_observed="quote_only",
        depth_fields_present=False,
        event_time=now - timedelta(milliseconds=20),
        quote_time=now - timedelta(milliseconds=20),
        submitted_at=now + timedelta(milliseconds=80),
        execution_completed_at=now + timedelta(milliseconds=150),
        episode_id=episode_id,
        disconnected=disconnected,
        allow_simulated_fixture=allow_simulated_fixture,
        exchange_timestamp_observed=exchange_timestamp_observed,
    )


def _load_validated_session(*, allow_new_entries: bool = True) -> PaperSessionConfig:
    """Validated deployed session config.

    The deployed file currently holds new entries (audit P0 hold). These tests
    prove M1 *capability*, so they opt back in explicitly; the hold itself is
    asserted separately in ``tests/test_audit_remediation.py``.
    """
    session_cfg = load_paper_session_config(ROOT / "config" / "paper_session.yaml")
    validated, _warnings = validate_startup_configuration(
        session_cfg, MODES, enforce_g3_shadow=True
    )
    assert validated.mode_stances["M1_CAS"] is ExecutionMode.PAPER
    if allow_new_entries:
        validated = validated.model_copy(update={"new_entries_enabled": True})
    return validated


def test_startup_keeps_m1_paper_without_latency_report() -> None:
    session_cfg = load_paper_session_config(ROOT / "config" / "paper_session.yaml")
    validated, warnings = validate_startup_configuration(
        session_cfg, MODES, enforce_g3_shadow=True
    )
    assert validated.mode_stances["M1_CAS"] is ExecutionMode.PAPER
    assert any("measured limitation" in item for item in warnings)


def test_m1_poll_tick_does_not_submit_without_event(
    store: TradingStore, clock: FrozenClock, tmp_path: Path
) -> None:
    underlying, call, instruments = _m1_chain()
    session_cfg = _load_validated_session()
    builder = _m1_fixture_builder(
        repo_root=tmp_path,
        session_cfg=session_cfg,
        option_candidates=(call,),
        underlying=underlying,
        instruments=instruments,
    )
    session = _session(
        store, clock, tmp_path, repo_root=tmp_path, session_cfg=session_cfg, builder=builder
    )
    result = session.tick()
    assert result is None or not session._runner.trade_manager.list_positions()


def test_m1_event_reaches_paper_fill_and_managed_exit(
    store: TradingStore, clock: FrozenClock, tmp_path: Path
) -> None:
    underlying, call, instruments = _m1_chain()
    session_cfg = _load_validated_session()
    builder = _m1_fixture_builder(
        repo_root=tmp_path,
        session_cfg=session_cfg,
        option_candidates=(call,),
        underlying=underlying,
        instruments=instruments,
    )
    session = _session(
        store, clock, tmp_path, repo_root=tmp_path, session_cfg=session_cfg, builder=builder
    )
    event = _simulated_event(now=clock.now_utc())
    fill = session.submit_m1_event(event)
    assert fill is not None
    assert any(
        any(evt.state is OrderState.FILLED for evt in outcome.order_events)
        for outcome in fill.outcomes
    )
    position = session._runner.trade_manager.list_positions()[0]
    assert position.mode_id is ModeId.M1_CAS
    assert position.state is TradeState.OPEN
    symbol = position.legs[0].contract.symbol
    stop_bid = f.price("28.90")
    exit_snap = call.model_copy(
        update={"market": f.quote(bid=stop_bid, ask=f.price("28.95"), last=stop_bid)}
    )
    session._runner.manage_exits({symbol: exit_snap})
    closed = session._runner.trade_manager.get_position(position.trade_id)
    assert closed is not None
    assert closed.state in {TradeState.EXIT_PENDING, TradeState.CLOSING, TradeState.CLOSED}


def test_m1_disconnect_and_missing_timestamp_abstain(
    store: TradingStore, clock: FrozenClock, tmp_path: Path
) -> None:
    underlying, call, instruments = _m1_chain()
    session_cfg = _load_validated_session()
    builder = _m1_fixture_builder(
        repo_root=tmp_path,
        session_cfg=session_cfg,
        option_candidates=(call,),
        underlying=underlying,
        instruments=instruments,
    )
    session = _session(
        store, clock, tmp_path, repo_root=tmp_path, session_cfg=session_cfg, builder=builder
    )
    disconnected = session.submit_m1_event(
        _simulated_event(now=clock.now_utc(), disconnected=True)
    )
    assert disconnected is None or not session._runner.trade_manager.list_positions()
    missing_ts = session.submit_m1_event(
        _simulated_event(
            now=clock.now_utc(),
            exchange_timestamp_observed=False,
            allow_simulated_fixture=False,
        )
    )
    assert missing_ts is None or not session._runner.trade_manager.list_positions()


def test_m1_repeated_episode_blocked_by_retry_limit(
    store: TradingStore, clock: FrozenClock, tmp_path: Path
) -> None:
    underlying, call, instruments = _m1_chain()
    session_cfg = _load_validated_session()
    session_cfg = session_cfg.model_copy(
        update={
            "cas_event_driven": CasEventDrivenConfig(
                enabled=True,
                episode_retry_limit=1,
            )
        }
    )
    builder = _m1_fixture_builder(
        repo_root=tmp_path,
        session_cfg=session_cfg,
        option_candidates=(call,),
        underlying=underlying,
        instruments=instruments,
    )
    session = _session(
        store, clock, tmp_path, repo_root=tmp_path, session_cfg=session_cfg, builder=builder
    )
    first = session.submit_m1_event(_simulated_event(now=clock.now_utc(), episode_id="dup"))
    assert first is not None
    second = session.submit_m1_event(_simulated_event(now=clock.now_utc(), episode_id="dup"))
    assert second is None or len(session._runner.trade_manager.list_positions()) == 1


def test_m1_restart_preserves_open_position(
    store: TradingStore, clock: FrozenClock, tmp_path: Path
) -> None:
    underlying, call, instruments = _m1_chain()
    session_cfg = _load_validated_session()
    builder = _m1_fixture_builder(
        repo_root=tmp_path,
        session_cfg=session_cfg,
        option_candidates=(call,),
        underlying=underlying,
        instruments=instruments,
    )
    session = _session(
        store, clock, tmp_path, repo_root=tmp_path, session_cfg=session_cfg, builder=builder
    )
    session.submit_m1_event(_simulated_event(now=clock.now_utc()))
    opened = session._runner.trade_manager.list_positions()[0]
    second = _restart(store, clock, session._runner.broker)
    recovery = second.recover_lifecycle()
    assert opened.trade_id in recovery.restored_trade_ids
    restored = second.trade_manager.get_position(opened.trade_id)
    assert restored is not None
    assert restored.state is TradeState.OPEN
