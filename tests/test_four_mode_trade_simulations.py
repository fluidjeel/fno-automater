"""Three end-to-end paper trade simulations per mode (M1-M4)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

import tests.factories as f
from tests.test_four_mode_session_integration import (
    _option_snap,
    _runner,
    _spec,
    _underlying,
)
from tests.test_p7_m1_selector_and_g3_path import (
    CAS_NOW,
)
from tests.test_p7_m1_selector_and_g3_path import (
    _market as m1_market,
)
from tests.test_p7_m1_selector_and_g3_path import (
    _option as m1_option,
)
from tests.test_p10_iron_condor_binder_and_g2 import (
    _condor_candidates,
)
from tests.test_p10_iron_condor_binder_and_g2 import (
    _macro as m4_macro,
)
from tests.test_p11_m4_broad_basket import _call_butterfly_candidates
from trading.domain.clock import FrozenClock
from trading.domain.contracts import FeatureSnapshot
from trading.domain.enums import (
    ExecutionMode,
    FamilyId,
    ModeId,
    OptionType,
    OrderState,
    ReasonCode,
    RiskAction,
)
from trading.identification import bind_m1_cas_option, load_identification_policy
from trading.news.contracts import EventRiskState
from trading.runtime.cas_event_path import (
    CasEventDrivenConfig,
    evaluate_cas_event_trigger,
    measure_cas_entry_latency,
)
from trading.runtime.paper_runner import PaperStrategyRequest
from trading.storage.trading_store import TradingStore
from trading.strategies.cas_microstructure import (
    FEATURE_AUCTION_IMBALANCE,
    FEATURE_MICROPRICE_EDGE_BPS,
    FEATURE_QUOTE_INSTABILITY,
    FEATURE_SET_VERSION,
    FEATURE_TRADE_FLOW_IMBALANCE,
)
from trading.strategies.macro import MacroAssessment, MacroBias

IDENT = load_identification_policy(
    Path(__file__).resolve().parent.parent / "config" / "identification.yaml"
)


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(f.NOW + timedelta(seconds=60))


@pytest.fixture
def cas_clock() -> FrozenClock:
    """Inside the M1 CAS window (15:00-15:30 IST)."""
    return FrozenClock(CAS_NOW + timedelta(seconds=60))


@pytest.fixture
def store(tmp_path: Path, clock: FrozenClock) -> Iterator[TradingStore]:
    trading_store = TradingStore.open(tmp_path / "sim.sqlite", clock=clock)
    yield trading_store
    trading_store.close()


def _macro(bias: MacroBias) -> MacroAssessment:
    return MacroAssessment(
        regime="RISK_ON",
        directional_bias=bias,
        confidence=Decimal("0.8"),
        fresh_until=f.NOW + timedelta(hours=1),
        model_version="test",
    )


def _cas_underlying(instant: datetime = CAS_NOW) -> FeatureSnapshot:
    times = f.snapshot_times(
        event_time=instant - timedelta(seconds=2),
        source_time=instant - timedelta(seconds=1),
        receive_time=instant - timedelta(milliseconds=500),
        calculation_time=instant - timedelta(milliseconds=200),
    )
    return f.snapshot(
        snapshot_id="SNAP-CAS-UNDER",
        contract=f.index_contract(),
        times=times,
        market=f.quote(last=f.price("24100"), close=f.price("24000")),
        feature_set_version=FEATURE_SET_VERSION,
        features={
            FEATURE_AUCTION_IMBALANCE: Decimal("0.40"),
            FEATURE_TRADE_FLOW_IMBALANCE: Decimal("0.30"),
            FEATURE_MICROPRICE_EDGE_BPS: Decimal("5.00"),
            FEATURE_QUOTE_INSTABILITY: Decimal("0.10"),
        },
    )


def _option(
    strike: str, option_type: OptionType, ask: str = "50.00"
) -> FeatureSnapshot:
    return _option_snap(strike, option_type, ask=ask)


def _request(
    *,
    strategy_id: str,
    candidates: tuple[FeatureSnapshot, ...],
    mode_id: ModeId,
    family_id: FamilyId,
    macro: MacroAssessment,
    underlying: FeatureSnapshot | None = None,
    event_risk_state: EventRiskState | None = None,
) -> PaperStrategyRequest:
    instruments = {
        c.contract.symbol: _spec(
            c.contract.symbol,
            str(c.contract.strike or "0"),
            c.contract.option_type or OptionType.CALL,
        )
        for c in candidates
    }
    return PaperStrategyRequest(
        strategy_id=strategy_id,
        underlying=underlying or _underlying(),
        candidates=candidates,
        instruments=instruments,
        event_risk_state=event_risk_state or f.event_risk_state(),
        experiment_id=f"EXP-{mode_id.value}-{family_id.value}",
        execution_mode=ExecutionMode.PAPER,
        macro=macro,
        execute=True,
        forced_mode_id=mode_id,
        forced_family_id=family_id,
    )


# --- M1 ---


def test_m1_sim_1_cas_long_call_paper_fill(
    store: TradingStore, cas_clock: FrozenClock
) -> None:
    base = m1_option(
        "NIFTY26SEP23700CE",
        strike="23700",
        delta="0.30",
        option_type=OptionType.CALL,
        bid="29.95",
        ask="30.00",
    )
    opt = base.model_copy(
        update={
            "market": base.market.model_copy(
                update={"bid_size": 300, "ask_size": 300},
            )
        }
    )
    macro = MacroAssessment(
        regime="RISK_ON",
        directional_bias=MacroBias.BULLISH,
        confidence=Decimal("0.8"),
        fresh_until=cas_clock.now_utc() + timedelta(hours=1),
        model_version="test",
    )
    now = cas_clock.now_utc()
    result = _runner(store, cas_clock).run_cycle(
        (
            _request(
                strategy_id="cas_microstructure",
                candidates=(opt,),
                mode_id=ModeId.M1_CAS,
                family_id=FamilyId.long_call,
                macro=macro,
                underlying=_cas_underlying(now),
                event_risk_state=f.event_risk_state(
                    as_of=now - timedelta(minutes=1),
                    expires_at=now + timedelta(hours=1),
                ),
            ),
        )
    )
    outcome = result.outcomes[0]
    assert outcome.intents[0].mode_id is ModeId.M1_CAS
    assert outcome.order_events[0].state is OrderState.FILLED


def test_m1_sim_2_event_stale_quote_blocked(clock: FrozenClock) -> None:
    opt = _option("23700", OptionType.CALL, ask="30.00")
    config = CasEventDrivenConfig(enabled=True, quote_max_age_ms=500)
    event_at = clock.now_utc()
    result = evaluate_cas_event_trigger(
        opt,
        config=config,
        event_at=event_at,
        now=event_at + timedelta(seconds=2),
    )
    assert not result.triggered
    assert ReasonCode.DATA_STALE in result.reason_codes


def test_m1_sim_3_binder_selects_otm_delta_band() -> None:
    from trading.domain.contracts.identification import TrendState

    m1 = m1_option(
        "NIFTY26SEP23700CE",
        strike="23700",
        delta="0.30",
        option_type=OptionType.CALL,
    )
    m2 = m1_option(
        "NIFTY26SEP24000CE",
        strike="24000",
        delta="0.55",
        option_type=OptionType.CALL,
    )
    bound = bind_m1_cas_option(
        (m1, m2),
        market=m1_market(TrendState.UP),
        policy=IDENT,
    )
    assert bound.binding.eligible
    assert "23700" in bound.candidates[0].contract.symbol


# --- M2 ---


def test_m2_sim_1_long_call_bullish_fill(
    store: TradingStore, clock: FrozenClock
) -> None:
    opt = _option("24000", OptionType.CALL)
    result = _runner(store, clock).run_cycle(
        (
            _request(
                strategy_id="positional_long_option",
                candidates=(opt,),
                mode_id=ModeId.M2_DIRECTIONAL,
                family_id=FamilyId.long_call,
                macro=_macro(MacroBias.BULLISH),
            ),
        )
    )
    outcome = result.outcomes[0]
    assert outcome.intents[0].mode_id is ModeId.M2_DIRECTIONAL
    assert outcome.decisions[0].action in {RiskAction.APPROVE, RiskAction.RESIZE}
    assert outcome.order_events[0].state is OrderState.FILLED


def test_m2_sim_2_long_put_bearish_fill(
    store: TradingStore, clock: FrozenClock
) -> None:
    opt = _option("24000", OptionType.PUT)
    result = _runner(store, clock).run_cycle(
        (
            _request(
                strategy_id="positional_long_option",
                candidates=(opt,),
                mode_id=ModeId.M2_DIRECTIONAL,
                family_id=FamilyId.long_put,
                macro=_macro(MacroBias.BEARISH),
                underlying=_underlying("23900"),
            ),
        )
    )
    assert result.outcomes[0].order_events


def test_m2_sim_3_neutral_macro_abstains(
    store: TradingStore, clock: FrozenClock
) -> None:
    opt = _option("24000", OptionType.CALL)
    result = _runner(store, clock).run_cycle(
        (
            _request(
                strategy_id="positional_long_option",
                candidates=(opt,),
                mode_id=ModeId.M2_DIRECTIONAL,
                family_id=FamilyId.long_call,
                macro=_macro(MacroBias.NEUTRAL),
                underlying=_underlying("24000"),
            ),
        )
    )
    assert result.outcomes[0].intents == ()


# --- M3 ---


def test_m3_sim_1_bull_call_debit_fill(
    store: TradingStore, clock: FrozenClock
) -> None:
    long_leg = _option("24000", OptionType.CALL, ask="50.00")
    short_leg = _option("24200", OptionType.CALL, ask="30.00")
    result = _runner(store, clock).run_cycle(
        (
            _request(
                strategy_id="debit_spread",
                candidates=(long_leg, short_leg),
                mode_id=ModeId.M3_TACTICAL_POSITIONAL,
                family_id=FamilyId.bull_call_debit,
                macro=_macro(MacroBias.BULLISH),
            ),
        )
    )
    outcome = result.outcomes[0]
    assert outcome.intents[0].mode_id is ModeId.M3_TACTICAL_POSITIONAL
    assert len(outcome.order_events) == 2


def test_m3_sim_2_bull_put_credit_fill(
    store: TradingStore, clock: FrozenClock
) -> None:
    long_leg = _option("23950", OptionType.PUT, ask="15.00")
    short_leg = _option("24000", OptionType.PUT, ask="35.00")
    result = _runner(store, clock).run_cycle(
        (
            _request(
                strategy_id="bull_put_credit",
                candidates=(short_leg, long_leg),
                mode_id=ModeId.M3_TACTICAL_POSITIONAL,
                family_id=FamilyId.bull_put_credit,
                macro=_macro(MacroBias.BULLISH),
            ),
        )
    )
    assert result.outcomes[0].order_events


def test_m3_sim_3_duplicate_suppressed_same_cycle(
    store: TradingStore, clock: FrozenClock
) -> None:
    long_leg = _option("24000", OptionType.CALL, ask="50.00")
    short_leg = _option("24200", OptionType.CALL, ask="30.00")
    req = _request(
        strategy_id="debit_spread",
        candidates=(long_leg, short_leg),
        mode_id=ModeId.M3_TACTICAL_POSITIONAL,
        family_id=FamilyId.bull_call_debit,
        macro=_macro(MacroBias.BULLISH),
    )
    dup = PaperStrategyRequest(
        strategy_id=req.strategy_id,
        underlying=req.underlying,
        candidates=req.candidates,
        instruments=req.instruments,
        event_risk_state=req.event_risk_state,
        experiment_id="EXP-DUP",
        execution_mode=req.execution_mode,
        macro=req.macro,
        execute=req.execute,
        forced_mode_id=req.forced_mode_id,
        forced_family_id=req.forced_family_id,
    )
    result = _runner(store, clock).run_cycle((req, dup))
    assert ReasonCode.EXACT_DUPLICATE_SUPPRESSED in result.outcomes[1].rejection_reasons


# --- M4 ---


def test_m4_sim_1_iron_condor_fill(store: TradingStore, clock: FrozenClock) -> None:
    candidates = _condor_candidates()
    instruments = {
        c.contract.symbol: _spec(
            c.contract.symbol,
            str(c.contract.strike or "0"),
            c.contract.option_type or OptionType.CALL,
        )
        for c in candidates
    }
    result = _runner(store, clock).run_cycle(
        (
            PaperStrategyRequest(
                strategy_id="iron_condor",
                underlying=f.snapshot(
                    snapshot_id="SNAP-UNDER",
                    contract=f.index_contract(),
                    market=f.quote(last=f.price("24500"), close=f.price("24500")),
                ),
                candidates=candidates,
                instruments=instruments,
                event_risk_state=f.event_risk_state(),
                experiment_id="EXP-M4-IC",
                execution_mode=ExecutionMode.PAPER,
                macro=m4_macro(),
                execute=True,
                forced_mode_id=ModeId.M4_STRATEGIC_POSITIONAL,
                forced_family_id=FamilyId.short_iron_condor_defined,
            ),
        )
    )
    outcome = result.outcomes[0]
    assert outcome.intents[0].mode_id is ModeId.M4_STRATEGIC_POSITIONAL
    assert len(outcome.order_events) == 4


def test_m4_sim_2_long_call_butterfly_fill(
    store: TradingStore, clock: FrozenClock
) -> None:
    candidates = _call_butterfly_candidates()
    instruments = {
        c.contract.symbol: _spec(
            c.contract.symbol,
            str(c.contract.strike or "0"),
            OptionType.CALL,
        )
        for c in candidates
    }
    result = _runner(store, clock).run_cycle(
        (
            PaperStrategyRequest(
                strategy_id=FamilyId.long_call_butterfly.value,
                underlying=f.snapshot(
                    snapshot_id="SNAP-UNDER",
                    contract=f.index_contract(),
                    market=f.quote(last=f.price("24500"), close=f.price("24500")),
                ),
                candidates=candidates,
                instruments=instruments,
                event_risk_state=f.event_risk_state(),
                experiment_id="EXP-M4-LCB",
                execution_mode=ExecutionMode.PAPER,
                macro=m4_macro(),
                execute=True,
                forced_mode_id=ModeId.M4_STRATEGIC_POSITIONAL,
                forced_family_id=FamilyId.long_call_butterfly,
            ),
        )
    )
    assert result.outcomes[0].order_events


def test_m4_sim_3_cas_latency_gate_passes() -> None:
    config = CasEventDrivenConfig(enabled=True, max_entry_latency_ms=2000)
    report = measure_cas_entry_latency((80, 120, 150), config=config)
    assert report.passes_session_gate
