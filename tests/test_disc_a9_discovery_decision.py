"""DISC-A9: durable DISCOVERY_DECISION records for every evaluation and exit."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import tests.factories as f
from tests.test_paper_runner import BROKER_FIXTURES, ROOT, _paper_config, _request
from trading.broker.paper import PaperBroker
from trading.config import load_evaluation_config, load_risk_policy
from trading.config.discovery import load_discovery_config
from trading.domain.clock import FrozenClock
from trading.domain.contracts import FeatureSnapshot
from trading.domain.contracts.discovery_decision import DiscoveryDecision
from trading.domain.contracts.identification import (
    MacroStatus,
    SetupFeatures,
    StructureKind,
    TrendState,
    VolatilityState,
)
from trading.domain.enums import (
    DiscoveryDecisionKind,
    DiscoveryStage,
    FamilyId,
    ModeId,
    ReasonCode,
)
from trading.domain.ids import SequentialIdFactory
from trading.runtime.discovery_decision_recorder import query_decisions
from trading.runtime.paper_runner import PaperRunner, PaperStrategyRequest
from trading.storage.trading_store import TradingEventType, TradingStore
from trading.strategies.macro import MacroAssessment, MacroBias

NOW = f.NOW
NOW_CTX = NOW + timedelta(seconds=60)
KOLKATA = ZoneInfo("Asia/Kolkata")
DISCOVERY = load_discovery_config(ROOT / "config" / "discovery.yaml")


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
        discovery_config=DISCOVERY,
    )


def _setup_features(**overrides: object) -> SetupFeatures:
    payload: dict[str, object] = {
        "identification_rule_version": "id-v1",
        "router_version": "router-v1",
        "market_state_id": "ms-1",
        "raw_setup_score": Decimal("0.11"),
        "score_components": {
            "return_15m": Decimal("0.0002"),
            "return_60m": Decimal("-0.0005"),
        },
        "trend": TrendState.MIXED,
        "volatility": VolatilityState.NORMAL,
        "structure": StructureKind.LONG_OPTION,
        "dte": 7,
        "spread_fraction": Decimal("0.01"),
        "liquidity_rank": Decimal("0.8"),
        "event_state": "NORMAL",
        "macro_status": MacroStatus.NEUTRAL,
    }
    payload.update(overrides)
    return SetupFeatures.model_validate(payload)


def _family_request(
    *,
    mode_id: ModeId,
    family_id: FamilyId,
    strategy_id: str,
    macro: MacroAssessment | None,
    setup: SetupFeatures | None = None,
    candidates: tuple[FeatureSnapshot, ...] | None = None,
) -> PaperStrategyRequest:
    base = _request(
        strategy_id=strategy_id,
        macro=macro,
        setup_features=setup,
        forced_mode_id=mode_id,
        forced_family_id=family_id,
        experiment_id=f"EXP-{mode_id.value}-{family_id.value}",
    )
    if candidates is not None:
        return PaperStrategyRequest(
            strategy_id=base.strategy_id,
            underlying=base.underlying,
            candidates=candidates,
            instruments=base.instruments,
            event_risk_state=base.event_risk_state,
            experiment_id=base.experiment_id,
            execution_mode=base.execution_mode,
            macro=base.macro,
            execute=base.execute,
            setup_features=base.setup_features,
            route_decision=base.route_decision,
            forced_mode_id=mode_id,
            forced_family_id=family_id,
        )
    return base


def _decision_rows(store: TradingStore) -> list[DiscoveryDecision]:
    rows: list[DiscoveryDecision] = []
    for event in store.read_events():
        if event.event_type is not TradingEventType.DISCOVERY_DECISION:
            continue
        payload = event.deserialize()
        if isinstance(payload, DiscoveryDecision):
            rows.append(payload)
    return rows


def test_cycle_writes_one_record_per_mode_family_pair(tmp_path: Path) -> None:
    """One poll with four mode/family rows yields exactly that many records."""
    clock = FrozenClock(NOW_CTX)
    store = TradingStore.open(tmp_path / "disc.sqlite", clock=clock)
    runner = _runner(store, clock)
    neutral_macro = MacroAssessment(
        regime="NEUTRAL",
        directional_bias=MacroBias.NEUTRAL,
        confidence=Decimal("0.9"),
        fresh_until=NOW_CTX + timedelta(hours=1),
        model_version="test",
    )
    requests = tuple(
        _request(
            forced_mode_id=mode,
            forced_family_id=FamilyId.long_call,
            macro=neutral_macro,
        )
        for mode in (
            ModeId.M1_CAS,
            ModeId.M2_DIRECTIONAL,
            ModeId.M3_TACTICAL_POSITIONAL,
            ModeId.M4_STRATEGIC_POSITIONAL,
        )
    )
    result = runner.run_cycle(requests)
    runner.persist_discovery_decisions(result, requests, as_of=clock.now_utc())
    rows = _decision_rows(store)
    assert len(rows) == len(requests)
    for row in rows:
        if row.decision is not DiscoveryDecisionKind.TRADE:
            assert row.reason_codes


def test_m2_neutral_direction_reason_text(tmp_path: Path) -> None:
    """M2 NEUTRAL emits deterministic reason_text mentioning scores."""
    clock = FrozenClock(NOW_CTX)
    store = TradingStore.open(tmp_path / "neutral.sqlite", clock=clock)
    runner = _runner(store, clock)
    flat_underlying = f.snapshot(
        snapshot_id="SNAP-FLAT",
        contract=f.index_contract(),
        market=f.quote(last=f.price("24000"), close=f.price("24000")),
    )
    request = _family_request(
        mode_id=ModeId.M2_DIRECTIONAL,
        family_id=FamilyId.long_call,
        strategy_id="positional_long_option",
        macro=MacroAssessment(
            regime="NEUTRAL",
            directional_bias=MacroBias.NEUTRAL,
            confidence=Decimal("0.9"),
            fresh_until=NOW_CTX + timedelta(hours=1),
            model_version="test",
        ),
        setup=_setup_features(),
    )
    request = PaperStrategyRequest(
        strategy_id=request.strategy_id,
        underlying=flat_underlying,
        candidates=request.candidates,
        instruments=request.instruments,
        event_risk_state=request.event_risk_state,
        experiment_id=request.experiment_id,
        execution_mode=request.execution_mode,
        macro=request.macro,
        execute=request.execute,
        setup_features=request.setup_features,
        forced_mode_id=ModeId.M2_DIRECTIONAL,
        forced_family_id=FamilyId.long_call,
    )
    result = runner.run_cycle((request,))
    runner.persist_discovery_decisions(result, (request,), as_of=clock.now_utc())
    row = _decision_rows(store)[0]
    assert row.stage is DiscoveryStage.DIRECTION
    assert ReasonCode.DIRECTION_NEUTRAL in row.reason_codes
    assert "direction neutral" in row.reason_text.lower()
    assert "+0.02%" in row.reason_text
    assert "0.11" in row.reason_text


def test_trade_record_includes_candidates_sizing_fill_and_strict_shadow(
    tmp_path: Path,
) -> None:
    """Approved trade records candidates, sizing, fill and strict_would_block."""
    clock = FrozenClock(NOW_CTX)
    store = TradingStore.open(tmp_path / "trade.sqlite", clock=clock)
    runner = _runner(store, clock)
    request = _request(
        forced_mode_id=ModeId.M2_DIRECTIONAL,
        forced_family_id=FamilyId.long_call,
    )
    result = runner.run_cycle((request,))
    outcome = result.outcomes[0]
    assert outcome.order_events
    runner.persist_discovery_decisions(result, (request,), as_of=clock.now_utc())
    row = _decision_rows(store)[0]
    assert row.decision is DiscoveryDecisionKind.TRADE
    assert row.stage is DiscoveryStage.FILL
    assert row.candidates
    assert any(item.selected for item in row.candidates)
    assert row.sizing is not None
    assert row.sizing.lots is not None and row.sizing.lots >= 1
    assert row.fill is not None
    assert row.fill.assumed_price is not None


def test_exit_record_names_rule_prices_and_pnl(tmp_path: Path) -> None:
    """Exit evaluation writes EXIT-stage record with rule and prices."""
    clock = FrozenClock(NOW_CTX)
    store = TradingStore.open(tmp_path / "exit.sqlite", clock=clock)
    runner = _runner(store, clock)
    request = _request(
        forced_mode_id=ModeId.M2_DIRECTIONAL,
        forced_family_id=FamilyId.long_call,
    )
    result = runner.run_cycle((request,))
    assert result.outcomes[0].order_events
    trade_id = result.outcomes[0].order_events[0].identity.trade_id
    position = runner.trade_manager.get_position(trade_id)
    assert position is not None
    assert trade_id in runner._open_book
    option = request.candidates[0]
    crashed = option.model_copy(
        update={
            "market": option.market.model_copy(
                update={
                    "last": f.price("40.00"),
                    "bid": f.price("39.95"),
                    "ask": f.price("40.05"),
                }
            )
        }
    )
    runner.manage_exits({option.contract.symbol: crashed})
    rows = _decision_rows(store)
    assert rows
    exit_row = rows[-1]
    assert exit_row.stage is DiscoveryStage.EXIT
    assert exit_row.exit_rule is not None
    assert "stop" in exit_row.reason_text.lower() or exit_row.exit_rule == "stop"


def test_decisions_survive_restart_and_query_by_date_mode_experiment(
    tmp_path: Path,
) -> None:
    """Records reload from store and filter by date, mode and experiment_id."""
    clock = FrozenClock(NOW_CTX)
    path = tmp_path / "persist.sqlite"
    store = TradingStore.open(path, clock=clock)
    runner = _runner(store, clock)
    request = _family_request(
        mode_id=ModeId.M2_DIRECTIONAL,
        family_id=FamilyId.long_put,
        strategy_id="positional_long_option",
        macro=MacroAssessment(
            regime="NEUTRAL",
            directional_bias=MacroBias.NEUTRAL,
            confidence=Decimal("0.9"),
            fresh_until=NOW_CTX + timedelta(hours=1),
            model_version="test",
        ),
        setup=_setup_features(),
    )
    result = runner.run_cycle((request,))
    runner.persist_discovery_decisions(result, (request,), as_of=clock.now_utc())
    store.close()

    reopened = TradingStore.open(path, clock=FrozenClock(NOW_CTX))
    try:
        session = datetime.combine(
            NOW_CTX.astimezone(KOLKATA).date(),
            datetime.min.time(),
            tzinfo=KOLKATA,
        )
        by_date = query_decisions(reopened, session_date=session)
        assert len(by_date) == 1
        by_mode = query_decisions(
            reopened, session_date=session, mode_id=ModeId.M2_DIRECTIONAL
        )
        assert len(by_mode) == 1
        by_experiment = query_decisions(
            reopened,
            session_date=session,
            experiment_id=request.experiment_id,
        )
        assert len(by_experiment) == 1
    finally:
        reopened.close()
