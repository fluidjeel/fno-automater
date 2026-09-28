"""DISC-A36: PAPER DISCOVERY duplicate entry suppression across restarts."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import cast
from zoneinfo import ZoneInfo

import pytest

import tests.factories as f
from tests.test_four_mode_session_integration import _runner as _strict_runner
from tests.test_four_mode_session_integration import _spec
from tests.test_p10_iron_condor_binder_and_g2 import _macro as m4_macro
from tests.test_p11_m4_broad_basket import _call_butterfly_candidates, _ctx
from tests.test_paper_runner import BROKER_FIXTURES, ROOT, _paper_config
from trading.broker.paper import PaperBroker
from trading.config import load_evaluation_config, load_risk_policy
from trading.config.discovery import load_discovery_config
from trading.config.loader import LoadedConfig
from trading.domain.clock import FrozenClock
from trading.domain.contracts import FeatureSnapshot
from trading.domain.contracts.instrument import InstrumentSpec
from trading.domain.contracts.intent import TradeIntent
from trading.domain.contracts.position import PositionState
from trading.domain.enums import (
    ExecutionMode,
    FamilyId,
    ModeId,
    OptionType,
    ReasonCode,
    TradeState,
)
from trading.domain.ids import SequentialIdFactory
from trading.portfolio.arbitration import PortfolioArbiter
from trading.portfolio.structure_dedup import filter_same_day_structure_duplicates
from trading.runtime.paper_runner import PaperRunner, PaperStrategyRequest
from trading.storage.trading_store import TradingStore
from trading.strategies.base import StrategyContext
from trading.strategies.m4_broad_basket import LongCallButterflyStrategy

DISCOVERY = load_discovery_config(ROOT / "config" / "discovery.yaml").config
NOW = f.NOW
KOLKATA = ZoneInfo("Asia/Kolkata")


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(NOW + timedelta(seconds=60))


@pytest.fixture
def store(tmp_path: Path, clock: FrozenClock) -> Iterator[TradingStore]:
    trading_store = TradingStore.open(tmp_path / "disc_a36.sqlite", clock=clock)
    yield trading_store
    trading_store.close()


def _spec_for(item: FeatureSnapshot) -> InstrumentSpec:
    return _spec(
        item.contract.symbol,
        str(item.contract.strike or "0"),
        item.contract.option_type or OptionType.CALL,
    )


def _butterfly_request(
    candidates: tuple[FeatureSnapshot, ...] | None = None,
) -> PaperStrategyRequest:
    chain = candidates or _call_butterfly_candidates()
    return PaperStrategyRequest(
        strategy_id=FamilyId.long_call_butterfly.value,
        underlying=f.snapshot(
            snapshot_id="SNAP-UNDER",
            contract=f.index_contract(),
            market=f.quote(last=f.price("24500"), close=f.price("24500")),
        ),
        candidates=chain,
        instruments={item.contract.symbol: _spec_for(item) for item in chain},
        event_risk_state=f.event_risk_state(),
        experiment_id="EXP-M4-LCB-A36",
        execution_mode=ExecutionMode.PAPER,
        macro=m4_macro(),
        execute=True,
        forced_mode_id=ModeId.M4_STRATEGIC_POSITIONAL,
        forced_family_id=FamilyId.long_call_butterfly,
    )


def _discovery_runner(
    store: TradingStore,
    clock: FrozenClock,
    *,
    id_factory: SequentialIdFactory | None = None,
) -> PaperRunner:
    ids = id_factory or SequentialIdFactory(clock.instant)
    fill_model = load_evaluation_config(
        ROOT / "config" / "evaluation.yaml"
    ).config.fill_model
    broker = PaperBroker.from_fixtures(
        BROKER_FIXTURES, clock=clock, id_factory=ids, fill_model=fill_model
    )
    return PaperRunner(
        account_config=cast(LoadedConfig, _paper_config()),
        risk_policy=load_risk_policy(ROOT / "config" / "risk.yaml"),
        store=store,
        broker=broker,
        clock=clock,
        id_factory=ids,
        fill_model=fill_model,
        discovery_config=DISCOVERY,
    )


def _butterfly_intent() -> TradeIntent:
    return (
        LongCallButterflyStrategy()
        .evaluate(_ctx(_call_butterfly_candidates()))
        .intents[0]
    )


def _open_butterfly_position(trade_id: str = "TRD-FLY-1") -> PositionState:
    intent = _butterfly_intent()
    low, mid, high = intent.legs
    return f.position_state(
        trade_id=trade_id,
        intent_id=intent.intent_id,
        strategy_id=intent.strategy_id,
        mode_id=ModeId.M4_STRATEGIC_POSITIONAL,
        state=TradeState.OPEN,
        exit_policy=f.exit_policy(trade_id=trade_id),
        legs=(
            f.position_leg_state(
                leg_id=low.leg_id,
                contract=low.contract,
                side=low.side,
                quantity_contracts=75,
            ),
            f.position_leg_state(
                leg_id=mid.leg_id,
                contract=mid.contract,
                side=mid.side,
                quantity_contracts=150,
            ),
            f.position_leg_state(
                leg_id=high.leg_id,
                contract=high.contract,
                side=high.side,
                quantity_contracts=75,
            ),
        ),
        opened_at=NOW,
    )


def test_butterfly_suppressed_against_open_position(clock: FrozenClock) -> None:
    """A 1:2:1 butterfly intent matches an open position with scaled quantities."""
    arbiter = PortfolioArbiter(max_m4_open_positions=4)
    intent = _butterfly_intent()
    position = _open_butterfly_position()

    result = arbiter.arbitrate(
        (intent,),
        existing_positions=(position,),
        now=clock.now_utc(),
    )

    assert len(result.approved_intents) == 0
    assert len(result.suppressed_intents) == 1
    assert (
        result.suppressed_intents[0].reason_code
        is ReasonCode.EXACT_DUPLICATE_SUPPRESSED
    )
    assert result.suppressed_intents[0].incumbent_id == position.trade_id


def test_restart_replay_four_sessions_one_butterfly(
    store: TradingStore, clock: FrozenClock
) -> None:
    """Four restarted sessions on the same chain produce exactly one butterfly."""
    request = _butterfly_request()
    intent_ids: set[str] = set()

    for session in range(4):
        ids = SequentialIdFactory(clock.instant + timedelta(seconds=session))
        runner = _discovery_runner(store, clock, id_factory=ids)
        result = runner.run_cycle((request,))
        outcome = result.outcomes[0]
        if outcome.intents:
            intent_ids.add(outcome.intents[0].intent_id)

    open_positions = [
        position
        for position in runner.trade_manager.list_positions()
        if position.state is TradeState.OPEN
    ]
    assert len(open_positions) == 1
    assert len(intent_ids) == 1


def test_closed_same_day_identical_structure_suppressed(
    store: TradingStore, clock: FrozenClock
) -> None:
    """A closed same-day lifecycle blocks an identical re-entry in DISCOVERY."""
    runner = _discovery_runner(store, clock)
    request = _butterfly_request()
    first = runner.run_cycle((request,))
    assert first.outcomes[0].order_events

    lifecycle = store.get_position_lifecycle(
        runner.trade_manager.list_positions()[0].trade_id
    )
    assert lifecycle is not None
    closed = lifecycle.model_copy(
        update={
            "position": lifecycle.position.model_copy(
                update={"state": TradeState.CLOSED}
            )
        }
    )
    store.upsert_position_lifecycle(closed, event_id="EVT-A36-CLOSE")

    clock.advance(timedelta(seconds=30))
    second = _discovery_runner(
        store, clock, id_factory=SequentialIdFactory(clock.instant)
    ).run_cycle((request,))
    assert ReasonCode.DUPLICATE_STRUCTURE in second.outcomes[0].rejection_reasons
    assert len(second.outcomes[0].order_events) == 0


def test_strict_closed_same_day_does_not_emit_duplicate_structure(
    store: TradingStore, clock: FrozenClock
) -> None:
    """STRICT keeps prior behaviour: no same-day DUPLICATE_STRUCTURE filter."""
    runner = _strict_runner(store, clock)
    request = _butterfly_request()
    first = runner.run_cycle((request,))
    assert first.outcomes[0].order_events

    lifecycle = store.get_position_lifecycle(
        runner.trade_manager.list_positions()[0].trade_id
    )
    assert lifecycle is not None
    store.upsert_position_lifecycle(
        lifecycle.model_copy(
            update={
                "position": lifecycle.position.model_copy(
                    update={"state": TradeState.CLOSED}
                )
            }
        ),
        event_id="EVT-A36-STRICT-CLOSE",
    )

    clock.advance(timedelta(seconds=30))
    second = _strict_runner(store, FrozenClock(clock.instant)).run_cycle((request,))
    assert ReasonCode.DUPLICATE_STRUCTURE not in second.outcomes[0].rejection_reasons


def test_m4_intent_id_is_deterministic_for_session(clock: FrozenClock) -> None:
    """M4 intent_id ignores snapshot_id and wall-clock jitter within a session."""
    first = _butterfly_intent()
    second = (
        LongCallButterflyStrategy()
        .evaluate(
            StrategyContext(
                underlying=f.snapshot(
                    snapshot_id="SNAP-B",
                    contract=f.index_contract(),
                    market=f.quote(last=f.price("24501"), close=f.price("24500")),
                ),
                candidates=_call_butterfly_candidates(),
                view=f.portfolio_view(),
                now=clock.now_utc(),
                macro=m4_macro(),
            )
        )
        .intents[0]
    )
    assert first.intent_id == second.intent_id


def test_structure_filter_rejects_duplicate_lifecycle(
    store: TradingStore, clock: FrozenClock
) -> None:
    """Same-day structure index suppresses an exact replay against lifecycle."""
    intent = _butterfly_intent()
    lifecycle = f.position_lifecycle_record(
        trade_id="TRD-LCB-1",
        position=_open_butterfly_position("TRD-LCB-1"),
        intent=intent,
    )
    store.upsert_position_lifecycle(lifecycle, event_id="EVT-A36-LCB")
    session_date = clock.now_utc().astimezone(KOLKATA).date()
    result = filter_same_day_structure_duplicates(
        (intent,),
        (lifecycle,),
        session_date=session_date,
    )
    assert result.kept == ()
    assert result.rejections[0][2] is ReasonCode.DUPLICATE_STRUCTURE
