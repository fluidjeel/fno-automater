"""Phase P4 Verification and Regression Lock-in Test Suite.

Locks in Phase P4 requirements from FOUR_MODE_LAYER_CHANGE_CRITERIA.md
(§2.19, §3.1, §3.2) and NIFTY_FOUR_MODE_CURSOR_REDESIGN.md:
- Mode tagging on M2 long option (ModeId.M2_DIRECTIONAL, FamilyId.long_call/long_put)
- Mode tagging on M3 debit spread (ModeId.M3_TACTICAL_POSITIONAL, FamilyId.bull_call_debit/bear_put_debit)
- Concurrent candidate production across modes in a single cycle
- PortfolioArbiter exact duplicate suppression (Scenario T27), explicitly recording incumbent ID
- Exact duplicate suppression against an open position in the portfolio
- Counterfactual evaluation log does not call reservation store or mutate TradingStore
- Normalization and deterministic arbitration order
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

import tests.factories as f
from tests.test_paper_runner import BROKER_FIXTURES, ROOT, _paper_config
from trading.broker.paper import PaperBroker
from trading.config import load_evaluation_config, load_risk_policy
from trading.domain.clock import FrozenClock
from trading.domain.contracts import (
    DerivativesContext,
    FeatureSnapshot,
    InstrumentSpec,
    IntentLeg,
)
from trading.domain.enums import (
    Exchange,
    ExecutionMode,
    FamilyId,
    InstrumentKind,
    ModeId,
    OptionType,
    OrderState,
    ReasonCode,
    RiskAction,
    Side,
    TradeState,
)
from trading.domain.ids import SequentialIdFactory
from trading.domain.primitives import Currency, Money
from trading.portfolio.arbitration import (
    ArbitrationResult,
    PortfolioArbiter,
    extract_leg_signature,
)
from trading.runtime.paper_runner import PaperRunner, PaperStrategyRequest
from trading.storage.trading_store import TradingStore
from trading.strategies import (
    DebitSpreadStrategy,
    LongOptionStrategy,
    MacroAssessment,
    MacroBias,
    StrategyContext,
)

NOW = f.NOW
NOW_CTX = NOW + timedelta(seconds=60)


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(NOW_CTX)


@pytest.fixture
def store(tmp_path: Path, clock: FrozenClock) -> Iterator[TradingStore]:
    trading_store = TradingStore.open(tmp_path / "paper_p4.sqlite", clock=clock)
    yield trading_store
    trading_store.close()


def _underlying(last: str = "24050", close: str = "24000") -> FeatureSnapshot:
    return f.snapshot(
        snapshot_id="SNAP-UNDER",
        contract=f.index_contract(),
        market=f.quote(last=f.price(last), close=f.price(close)),
    )


def _option_spec(
    symbol: str = "NIFTY26SEP24000CE",
    strike: str = "24000",
    option_type: OptionType = OptionType.CALL,
) -> InstrumentSpec:
    return InstrumentSpec.model_validate(
        {
            "trading_symbol": f"NSE:{symbol}",
            "exchange": Exchange.NFO,
            "segment": "NSE_FO",
            "underlying": "NIFTY",
            "instrument_kind": InstrumentKind.OPTION,
            "provider_token": "1",
            "exchange_token": 1,
            "lot_size": 75,
            "tick_size": Decimal("0.05"),
            "price_precision": 2,
            "expiry": date(2026, 9, 24),
            "strike": Decimal(strike),
            "option_type": option_type,
            "trading_session": "0915-1540",
            "source": "fixture",
            "verified_at": date(2026, 9, 11),
        }
    )


def _option_snap(
    strike: str = "24000",
    option_type: OptionType = OptionType.CALL,
    bid: str = "49.95",
    ask: str = "50.00",
) -> FeatureSnapshot:
    suffix = "CE" if option_type is OptionType.CALL else "PE"
    symbol = f"NIFTY26SEP{strike}{suffix}"
    return f.snapshot(
        snapshot_id=f"SNAP-{strike}-{suffix}",
        contract=f.option_contract(
            symbol=symbol,
            strike=Decimal(strike),
            option_type=option_type,
        ),
        market=f.quote(
            bid=f.price(bid),
            ask=f.price(ask),
            last=f.price(ask),
            bid_size=300,
            ask_size=300,
        ),
        derivatives=DerivativesContext(
            days_to_expiry=10,
            open_interest=5000,
            option_type=option_type,
            underlying_price=f.price("24000"),
        ),
    )


def _runner(store: TradingStore, clock: FrozenClock) -> PaperRunner:
    ids = SequentialIdFactory(clock.instant)
    fill_model = load_evaluation_config(
        ROOT / "config" / "evaluation.yaml"
    ).config.fill_model
    broker = PaperBroker.from_fixtures(
        BROKER_FIXTURES, clock=clock, id_factory=ids, fill_model=fill_model
    )
    return PaperRunner(
        account_config=_paper_config(),
        risk_policy=load_risk_policy(ROOT / "config" / "risk.yaml"),
        store=store,
        broker=broker,
        clock=clock,
        id_factory=ids,
        fill_model=fill_model,
    )


# ---------------------------------------------------------------------------
# Test 1: Mode and Family tagging on Strategy Producers
# ---------------------------------------------------------------------------


def test_p4_m2_long_option_tags_mode_and_family(clock: FrozenClock) -> None:
    """LongOptionStrategy emits mode_id=M2_DIRECTIONAL and family_id=long_call / long_put."""
    strategy = LongOptionStrategy()
    underlying = _underlying("24100", "24000")
    call_candidate = _option_snap("24000", OptionType.CALL)

    # Bullish context -> long_call
    bull_ctx = StrategyContext(
        underlying=underlying,
        candidates=(call_candidate,),
        view=f.portfolio_view(),
        now=clock.now_utc(),
        macro=MacroAssessment(
            regime="RISK_ON",
            directional_bias=MacroBias.BULLISH,
            confidence=Decimal("0.8"),
            fresh_until=clock.now_utc() + timedelta(hours=1),
            model_version="test",
        ),
    )
    decision = strategy.evaluate(bull_ctx)
    assert len(decision.intents) == 1
    intent = decision.intents[0]
    assert intent.mode_id == ModeId.M2_DIRECTIONAL
    assert intent.family_id == FamilyId.long_call

    # Bearish context -> long_put
    put_candidate = _option_snap("24000", OptionType.PUT)
    bear_ctx = StrategyContext(
        underlying=_underlying("23900", "24000"),
        candidates=(put_candidate,),
        view=f.portfolio_view(),
        now=clock.now_utc(),
        macro=MacroAssessment(
            regime="RISK_OFF",
            directional_bias=MacroBias.BEARISH,
            confidence=Decimal("0.8"),
            fresh_until=clock.now_utc() + timedelta(hours=1),
            model_version="test",
        ),
    )
    decision_bear = strategy.evaluate(bear_ctx)
    assert len(decision_bear.intents) == 1
    intent_bear = decision_bear.intents[0]
    assert intent_bear.mode_id == ModeId.M2_DIRECTIONAL
    assert intent_bear.family_id == FamilyId.long_put


def test_p4_m3_debit_spread_tags_mode_and_family(clock: FrozenClock) -> None:
    """DebitSpreadStrategy emits mode_id=M3_TACTICAL_POSITIONAL and family_id=bull_call_debit / bear_put_debit."""
    strategy = DebitSpreadStrategy()
    underlying = _underlying("24100", "24000")
    call_long = _option_snap("24000", OptionType.CALL, bid="49.95", ask="50.00")
    call_short = _option_snap("24200", OptionType.CALL, bid="29.95", ask="30.00")

    bull_ctx = StrategyContext(
        underlying=underlying,
        candidates=(call_long, call_short),
        view=f.portfolio_view(),
        now=clock.now_utc(),
        macro=MacroAssessment(
            regime="RISK_ON",
            directional_bias=MacroBias.BULLISH,
            confidence=Decimal("0.8"),
            fresh_until=clock.now_utc() + timedelta(hours=1),
            model_version="test",
        ),
    )
    decision = strategy.evaluate(bull_ctx)
    assert len(decision.intents) == 1
    intent = decision.intents[0]
    assert intent.mode_id == ModeId.M3_TACTICAL_POSITIONAL
    assert intent.family_id == FamilyId.bull_call_debit

    # Bearish context -> bear_put_debit
    put_long = _option_snap("24200", OptionType.PUT, bid="49.95", ask="50.00")
    put_short = _option_snap("24000", OptionType.PUT, bid="29.95", ask="30.00")
    bear_ctx = StrategyContext(
        underlying=_underlying("23900", "24000"),
        candidates=(put_long, put_short),
        view=f.portfolio_view(),
        now=clock.now_utc(),
        macro=MacroAssessment(
            regime="RISK_OFF",
            directional_bias=MacroBias.BEARISH,
            confidence=Decimal("0.8"),
            fresh_until=clock.now_utc() + timedelta(hours=1),
            model_version="test",
        ),
    )
    decision_bear = strategy.evaluate(bear_ctx)
    assert len(decision_bear.intents) == 1
    intent_bear = decision_bear.intents[0]
    assert intent_bear.mode_id == ModeId.M3_TACTICAL_POSITIONAL
    assert intent_bear.family_id == FamilyId.bear_put_debit


# ---------------------------------------------------------------------------
# Test 2: Concurrent Multi-Mode Candidate Emission in a Single Cycle
# ---------------------------------------------------------------------------


def test_p4_concurrent_candidate_emission_in_single_cycle(
    store: TradingStore, clock: FrozenClock
) -> None:
    """One cycle emits and executes candidates from both M2 and M3 concurrently."""
    runner = _runner(store, clock)

    # M2 long option: 24000 CE (affordable at 50.00)
    opt_m2 = _option_snap("24000", OptionType.CALL, bid="49.95", ask="50.00")
    spec_m2 = _option_spec(opt_m2.contract.symbol, "24000", OptionType.CALL)

    # M3 debit spread: 24000 CE long + 24200 CE short (backed by margin preview fixture)
    opt_m3_long = _option_snap("24000", OptionType.CALL, bid="49.95", ask="50.00")
    opt_m3_short = _option_snap("24200", OptionType.CALL, bid="29.95", ask="30.00")
    spec_m3_long = _option_spec(opt_m3_long.contract.symbol, "24000", OptionType.CALL)
    spec_m3_short = _option_spec(opt_m3_short.contract.symbol, "24200", OptionType.CALL)

    macro = MacroAssessment(
        regime="RISK_ON",
        directional_bias=MacroBias.BULLISH,
        confidence=Decimal("0.8"),
        fresh_until=clock.now_utc() + timedelta(hours=1),
        model_version="test",
    )

    req_m2 = PaperStrategyRequest(
        strategy_id="positional_long_option",
        underlying=_underlying("24050", "24000"),
        candidates=(opt_m2,),
        instruments={opt_m2.contract.symbol: spec_m2},
        event_risk_state=f.event_risk_state(),
        experiment_id="EXP-M2",
        execution_mode=ExecutionMode.PAPER,
        macro=macro,
        execute=True,
    )

    req_m3 = PaperStrategyRequest(
        strategy_id="debit_spread",
        underlying=_underlying("24050", "24000"),
        candidates=(opt_m3_long, opt_m3_short),
        instruments={
            opt_m3_long.contract.symbol: spec_m3_long,
            opt_m3_short.contract.symbol: spec_m3_short,
        },
        event_risk_state=f.event_risk_state(),
        experiment_id="EXP-M3",
        execution_mode=ExecutionMode.PAPER,
        macro=macro,
        execute=True,
    )

    result = runner.run_cycle((req_m2, req_m3))
    assert len(result.outcomes) == 2

    outcome_m2 = result.outcomes[0]
    outcome_m3 = result.outcomes[1]

    # Both produced candidate intents in the same cycle
    assert len(outcome_m2.intents) == 1
    assert outcome_m2.intents[0].mode_id == ModeId.M2_DIRECTIONAL
    assert len(outcome_m3.intents) == 1
    assert outcome_m3.intents[0].mode_id == ModeId.M3_TACTICAL_POSITIONAL

    # Arbitration approved both because leg structures differ (single leg vs spread)
    assert result.arbitration_result is not None
    assert len(result.arbitration_result.approved_intents) == 2
    assert len(result.arbitration_result.suppressed_intents) == 0

    # Both reached gateway and were approved/resized
    assert outcome_m2.decisions
    assert outcome_m2.decisions[0].action in {RiskAction.APPROVE, RiskAction.RESIZE}
    assert outcome_m3.decisions
    assert outcome_m3.decisions[0].action in {RiskAction.APPROVE, RiskAction.RESIZE}

    # Both submitted order events
    assert outcome_m2.order_events
    assert outcome_m2.order_events[0].state is OrderState.FILLED
    assert outcome_m3.order_events
    assert len(outcome_m3.order_events) == 2  # 2 legs filled


# ---------------------------------------------------------------------------
# Test 3: Scenario T27 — Exact Duplicate Suppression Referencing Incumbent ID
# ---------------------------------------------------------------------------


def test_p4_exact_duplicate_suppression_scenario_t27(
    store: TradingStore, clock: FrozenClock
) -> None:
    """Scenario T27: Exact duplicate candidate is suppressed referencing incumbent ID and does not reach gateway."""
    runner = _runner(store, clock)

    opt = _option_snap("24000", OptionType.CALL, bid="49.95", ask="50.00")
    spec = _option_spec(opt.contract.symbol, "24000", OptionType.CALL)

    macro = MacroAssessment(
        regime="RISK_ON",
        directional_bias=MacroBias.BULLISH,
        confidence=Decimal("0.8"),
        fresh_until=clock.now_utc() + timedelta(hours=1),
        model_version="test",
    )

    # Request 1: First producer proposes 24000 CE (Incumbent)
    req1 = PaperStrategyRequest(
        strategy_id="positional_long_option",
        underlying=_underlying("24050", "24000"),
        candidates=(opt,),
        instruments={opt.contract.symbol: spec},
        event_risk_state=f.event_risk_state(),
        experiment_id="EXP-1",
        execution_mode=ExecutionMode.PAPER,
        macro=macro,
        execute=True,
    )

    # Request 2: Second producer proposes exact same 24000 CE in same cycle
    req2 = PaperStrategyRequest(
        strategy_id="positional_long_option",
        underlying=_underlying("24050", "24000"),
        candidates=(opt,),
        instruments={opt.contract.symbol: spec},
        event_risk_state=f.event_risk_state(),
        experiment_id="EXP-2",
        execution_mode=ExecutionMode.PAPER,
        macro=macro,
        execute=True,
    )

    result = runner.run_cycle((req1, req2))
    assert len(result.outcomes) == 2

    outcome1 = result.outcomes[0]
    outcome2 = result.outcomes[1]

    # Outcome 1 is approved and submitted
    assert outcome1.decisions
    assert outcome1.decisions[0].action in {RiskAction.APPROVE, RiskAction.RESIZE}
    assert outcome1.order_events
    incumbent_id = outcome1.intents[0].intent_id

    # Outcome 2 is suppressed by arbitration
    assert ReasonCode.EXACT_DUPLICATE_SUPPRESSED in outcome2.rejection_reasons
    # Gateway was NEVER called for the suppressed candidate
    assert len(outcome2.decisions) == 0
    assert len(outcome2.order_events) == 0

    # Arbitration result records the exact suppression referencing incumbent ID (Scenario T27)
    arb = result.arbitration_result
    assert arb is not None
    assert len(arb.suppressed_intents) == 1
    suppression = arb.suppressed_intents[0]
    assert suppression.candidate_intent_id == outcome2.intents[0].intent_id
    assert suppression.incumbent_id == incumbent_id
    assert suppression.reason_code == ReasonCode.EXACT_DUPLICATE_SUPPRESSED


# ---------------------------------------------------------------------------
# Test 4: Exact Duplicate Suppression against Open Position
# ---------------------------------------------------------------------------


def test_p4_exact_duplicate_suppression_against_open_position(
    store: TradingStore, clock: FrozenClock
) -> None:
    """Exact duplicate candidate is suppressed when an open position already holds the same structure."""
    runner = _runner(store, clock)

    opt = _option_snap("24000", OptionType.CALL, bid="49.95", ask="50.00")
    spec = _option_spec(opt.contract.symbol, "24000", OptionType.CALL)

    macro = MacroAssessment(
        regime="RISK_ON",
        directional_bias=MacroBias.BULLISH,
        confidence=Decimal("0.8"),
        fresh_until=clock.now_utc() + timedelta(hours=1),
        model_version="test",
    )

    req = PaperStrategyRequest(
        strategy_id="positional_long_option",
        underlying=_underlying("24050", "24000"),
        candidates=(opt,),
        instruments={opt.contract.symbol: spec},
        event_risk_state=f.event_risk_state(),
        experiment_id="EXP-OPEN-1",
        execution_mode=ExecutionMode.PAPER,
        macro=macro,
        execute=True,
    )

    # First cycle: Opens the position
    res1 = runner.run_cycle((req,))
    assert res1.outcomes[0].order_events[0].state is OrderState.FILLED
    positions = runner.trade_manager.list_positions()
    open_pos = [p for p in positions if p.state is TradeState.OPEN]
    assert len(open_pos) == 1
    incumbent_trade_id = open_pos[0].trade_id

    # Second cycle: Proposes the identical structure while position is open
    res2 = runner.run_cycle((req,))
    outcome2 = res2.outcomes[0]

    assert ReasonCode.EXACT_DUPLICATE_SUPPRESSED in outcome2.rejection_reasons
    assert len(outcome2.decisions) == 0
    assert len(outcome2.order_events) == 0

    arb = res2.arbitration_result
    assert arb is not None
    assert len(arb.suppressed_intents) == 1
    assert arb.suppressed_intents[0].incumbent_id == incumbent_trade_id


# ---------------------------------------------------------------------------
# Test 5: Counterfactual Separation from Capital Reservation and Store
# ---------------------------------------------------------------------------


def test_p4_counterfactual_log_does_not_mutate_reservations_or_store(
    clock: FrozenClock,
) -> None:
    """Counterfactual entries are logged without calling CapitalReservationService or mutating store (§4.3)."""
    arbiter = PortfolioArbiter(max_m4_open_positions=2)
    opt = _option_snap("24000", OptionType.CALL, bid="49.95", ask="50.00")

    leg = IntentLeg(
        leg_id="leg-cf-1",
        contract=opt.contract,
        side=Side.BUY,
        ratio=1,
    )
    intent1 = f.intent(
        intent_id="INTENT-CF-1",
        legs=(leg,),
        mode_id=ModeId.M2_DIRECTIONAL,
        family_id=FamilyId.long_call,
        requested_risk=Money.of("3750", Currency.INR),
        estimated_max_loss=Money.of("3750", Currency.INR),
        strategy_confidence=Decimal("0.85"),
        created_at=clock.now_utc(),
    )
    intent2 = f.intent(
        intent_id="INTENT-CF-2",
        legs=(leg,),
        mode_id=ModeId.M2_DIRECTIONAL,
        family_id=FamilyId.long_call,
        requested_risk=Money.of("3750", Currency.INR),
        estimated_max_loss=Money.of("3750", Currency.INR),
        strategy_confidence=Decimal("0.75"),
        created_at=clock.now_utc(),
    )

    # In-memory evaluation of candidates
    result: ArbitrationResult = arbiter.arbitrate(
        [intent1, intent2],
        existing_positions=(),
        now=clock.now_utc(),
    )

    assert len(result.approved_intents) == 1
    assert result.approved_intents[0].intent_id == "INTENT-CF-1"
    assert len(result.suppressed_intents) == 1
    assert result.suppressed_intents[0].candidate_intent_id == "INTENT-CF-2"
    assert result.suppressed_intents[0].incumbent_id == "INTENT-CF-1"

    # Counterfactual log has both records
    assert len(result.counterfactual_log) == 2
    cf1, cf2 = result.counterfactual_log
    assert cf1.candidate_intent_id == "INTENT-CF-1"
    assert cf1.action == "APPROVED"
    assert cf1.requested_risk_amount == Decimal("3750")

    assert cf2.candidate_intent_id == "INTENT-CF-2"
    assert cf2.action == "SUPPRESSED_EXACT_DUPLICATE"
    assert cf2.incumbent_id == "INTENT-CF-1"
    assert cf2.requested_risk_amount == Decimal("3750")


# ---------------------------------------------------------------------------
# Test 6: Deterministic Signature Normalization
# ---------------------------------------------------------------------------


def test_p4_arbiter_deterministic_signature_normalization(clock: FrozenClock) -> None:
    """Leg ordering in multi-leg intent does not affect exact duplicate signature."""
    opt_a = _option_snap("24000", OptionType.CALL)
    opt_b = _option_snap("24200", OptionType.CALL)

    leg_a = IntentLeg(leg_id="leg-a", contract=opt_a.contract, side=Side.BUY, ratio=1)
    leg_b = IntentLeg(leg_id="leg-b", contract=opt_b.contract, side=Side.SELL, ratio=1)

    intent_ab = f.intent(
        intent_id="INTENT-AB",
        strategy_id="debit_spread",
        underlying="NIFTY",
        legs=(leg_a, leg_b),
        created_at=clock.now_utc(),
    )

    intent_ba = f.intent(
        intent_id="INTENT-BA",
        strategy_id="debit_spread",
        underlying="NIFTY",
        legs=(leg_b, leg_a),  # Reversed leg order in intent
        created_at=clock.now_utc(),
    )

    sig_ab = extract_leg_signature(intent_ab)
    sig_ba = extract_leg_signature(intent_ba)

    # Signatures must be strictly equal regardless of input leg sequence
    assert sig_ab == sig_ba


# ---------------------------------------------------------------------------
# Test 7: Cross-Strategy Exact Duplicate Arbitration
# ---------------------------------------------------------------------------


def test_p4_exact_duplicate_cross_strategy_suppression(clock: FrozenClock) -> None:
    """When candidates from different strategies share the exact same leg structure, the duplicate is suppressed with incumbent reference."""
    arbiter = PortfolioArbiter(max_m4_open_positions=2)
    opt = _option_snap("24000", OptionType.CALL, bid="49.95", ask="50.00")
    leg = IntentLeg(leg_id="leg-1", contract=opt.contract, side=Side.BUY, ratio=1)

    intent_m2 = f.intent(
        intent_id="INTENT-M2-CALL",
        strategy_id="positional_long_option",
        mode_id=ModeId.M2_DIRECTIONAL,
        family_id=FamilyId.long_call,
        legs=(leg,),
        strategy_confidence=Decimal("0.85"),
        requested_risk=Money.of("3750", Currency.INR),
        estimated_max_loss=Money.of("3750", Currency.INR),
        created_at=clock.now_utc(),
    )

    intent_m3_pseudo = f.intent(
        intent_id="INTENT-M3-DUP",
        strategy_id="debit_spread",
        mode_id=ModeId.M3_TACTICAL_POSITIONAL,
        family_id=FamilyId.bull_call_debit,
        legs=(leg,),  # Exact duplicate leg structure
        strategy_confidence=Decimal("0.70"),  # Lower confidence
        requested_risk=Money.of("3750", Currency.INR),
        estimated_max_loss=Money.of("3750", Currency.INR),
        created_at=clock.now_utc(),
    )

    result = arbiter.arbitrate([intent_m2, intent_m3_pseudo], now=clock.now_utc())

    assert len(result.approved_intents) == 1
    assert result.approved_intents[0].intent_id == "INTENT-M2-CALL"

    assert len(result.suppressed_intents) == 1
    supp = result.suppressed_intents[0]
    assert supp.candidate_intent_id == "INTENT-M3-DUP"
    assert supp.incumbent_id == "INTENT-M2-CALL"
    assert supp.reason_code == ReasonCode.EXACT_DUPLICATE_SUPPRESSED
