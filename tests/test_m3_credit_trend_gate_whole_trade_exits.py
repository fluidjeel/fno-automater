"""M3 credit trend gate and whole-trade (combined P&L) exits for multi-leg.

Oracle paper 2026-10-05..13: bull put credits bound with trend UNKNOWN, and
multi-leg structures exited on one leg's own stop/target (a hedge-leg stop on
an in-profit credit spread; a "target" on the long put of a losing spread).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

import tests.factories as f
from tests.test_disc_a21_discovery_exits import (
    CYCLE,
    DISCOVERY,
    EXITS,
    NOW,
    QTY,
    _mid_snapshot,
)
from tests.test_p6_credit_verticals_and_g2 import (
    IDENTIFICATION_POLICY,
    _market,
    _option,
)
from tests.test_paper_runner import BROKER_FIXTURES, ROOT, _paper_config
from trading.broker.paper import PaperBroker
from trading.config import load_risk_policy
from trading.config.discovery import DiscoveryExitsConfig
from trading.domain.clock import FrozenClock
from trading.domain.contracts import (
    FeatureSnapshot,
    IntentLeg,
    MarketState,
    PositionLegState,
    PositionState,
    TradeIntent,
)
from trading.domain.contracts.identification import TrendState
from trading.domain.enums import (
    ExitScope,
    FamilyId,
    OptionType,
    ReasonCode,
    Side,
    TradeState,
)
from trading.domain.ids import SequentialIdFactory
from trading.domain.primitives import Currency, Money
from trading.identification import bind_credit_spread
from trading.identification.binders import BoundCandidates
from trading.runtime.paper_runner import PaperRunner
from trading.storage.trading_store import TradingStore
from trading.trade.discovery_exits import (
    apply_discovery_leg_exit_prices,
    build_discovery_exit_policy,
    structure_pnl_bounds,
)
from trading.trade.exits import ExitEngine, ExitEvaluation, ExitKind

EXPIRY = date(2026, 10, 13)


# ---------------------------------------------------------------- trend gate
def _puts() -> tuple[FeatureSnapshot, FeatureSnapshot]:
    return (
        _option(
            "NIFTY_23950_PE",
            strike="23950",
            delta="-0.20",
            expiry=date(2026, 10, 1),
            dte=7,
            bid="15.00",
            ask="15.10",
            option_type=OptionType.PUT,
        ),
        _option(
            "NIFTY_24000_PE",
            strike="24000",
            delta="-0.30",
            expiry=date(2026, 10, 1),
            dte=7,
            bid="35.00",
            ask="35.10",
            option_type=OptionType.PUT,
        ),
    )


def _calls() -> tuple[FeatureSnapshot, FeatureSnapshot]:
    return (
        _option(
            "NIFTY_24000_CE",
            strike="24000",
            delta="0.30",
            expiry=date(2026, 10, 1),
            dte=7,
            bid="35.00",
            ask="35.10",
            option_type=OptionType.CALL,
        ),
        _option(
            "NIFTY_24050_CE",
            strike="24050",
            delta="0.20",
            expiry=date(2026, 10, 1),
            dte=7,
            bid="15.00",
            ask="15.10",
            option_type=OptionType.CALL,
        ),
    )


def _unknown_up_half_percent() -> MarketState:
    """Trend UNKNOWN (warm-up incomplete) while Nifty is +0.5% on the day."""
    return _market(
        TrendState.UNKNOWN,
        warmup_complete=False,
        return_15m=None,
        return_60m=None,
        trend_score=None,
        close_relative_move=Decimal("0.005"),
    )


def _assert_rejected_before_intent(bound: BoundCandidates) -> None:
    binding = bound.binding
    assert binding.eligible is False
    assert binding.reason_codes == (ReasonCode.DIRECTION_UNRESOLVED,)
    assert binding.selected_symbols == ()
    # Nothing to build an intent or order from.
    assert bound.candidates == ()
    assert bound.setup_features is None


@pytest.mark.parametrize(
    "market",
    [_market(TrendState.DOWN), _unknown_up_half_percent()],
    ids=["trend_down", "unknown_plus_half_percent"],
)
def test_bull_put_credit_rejected_without_up_trend(market: MarketState) -> None:
    bound = bind_credit_spread(
        _puts(),
        market=market,
        policy=IDENTIFICATION_POLICY,
        family_id=FamilyId.bull_put_credit,
    )
    _assert_rejected_before_intent(bound)


@pytest.mark.parametrize(
    "market",
    [_market(TrendState.UP), _unknown_up_half_percent()],
    ids=["trend_up", "unknown"],
)
def test_bear_call_credit_rejected_without_down_trend(market: MarketState) -> None:
    bound = bind_credit_spread(
        _calls(),
        market=market,
        policy=IDENTIFICATION_POLICY,
        family_id=FamilyId.bear_call_credit,
    )
    _assert_rejected_before_intent(bound)


def test_credit_happy_paths_still_bind() -> None:
    bull = bind_credit_spread(
        _puts(),
        market=_market(TrendState.UP),
        policy=IDENTIFICATION_POLICY,
        family_id=FamilyId.bull_put_credit,
    )
    bear = bind_credit_spread(
        _calls(),
        market=_market(TrendState.DOWN),
        policy=IDENTIFICATION_POLICY,
        family_id=FamilyId.bear_call_credit,
    )
    assert bull.binding.eligible is True
    assert bear.binding.eligible is True


# ------------------------------------------------------- whole-trade exits
def _leg(
    leg_id: str, side: Side, strike: str, entry: str, option_type: OptionType
) -> PositionLegState:
    kind = "PE" if option_type is OptionType.PUT else "CE"
    return PositionLegState(
        leg_id=leg_id,
        contract=f.option_contract(
            symbol=f"NIFTY26O13{strike}{kind}",
            strike=Decimal(strike),
            option_type=option_type,
            expiry=EXPIRY,
        ),
        side=side,
        quantity_contracts=QTY,
        average_entry_price=f.price(entry),
    )


def _bull_put() -> tuple[PositionLegState, ...]:
    """Oracle TRD-...00017: BUY 22200P @67.45, SELL 22400P @111.70, qty 65."""
    return (
        _leg("leg-long", Side.BUY, "22200", "67.45", OptionType.PUT),
        _leg("leg-short", Side.SELL, "22400", "111.70", OptionType.PUT),
    )


def _straddle() -> tuple[PositionLegState, ...]:
    """Oracle TRD-...04104: BUY 22750C @154.65 + BUY 22750P @180.65."""
    return (
        _leg("leg-call", Side.BUY, "22750", "154.65", OptionType.CALL),
        _leg("leg-put", Side.BUY, "22750", "180.65", OptionType.PUT),
    )


def _intent(legs: tuple[PositionLegState, ...], max_loss: str) -> TradeIntent:
    return f.intent(
        legs=tuple(
            IntentLeg(leg_id=leg.leg_id, contract=leg.contract, side=leg.side, ratio=1)
            for leg in legs
        ),
        estimated_max_loss=Money.of(max_loss, Currency.INR),
    )


def _open(
    raw: tuple[PositionLegState, ...], max_loss: str, trade_id: str
) -> tuple[PositionState, TradeIntent]:
    legs = apply_discovery_leg_exit_prices(raw, EXITS)
    intent = _intent(legs, max_loss)
    policy = build_discovery_exit_policy(
        intent,
        legs,
        f.exit_template(stop_distance_ticks=40),
        trade_id=trade_id,
        policy_id=f"EXIT-{trade_id}",
        initialized_at=NOW,
        config=EXITS,
        scope=ExitScope.STRATEGY_PNL,
        max_loss=intent.estimated_max_loss,
    )
    position = f.position_state(
        trade_id=trade_id,
        state=TradeState.OPEN,
        legs=legs,
        exit_policy=policy,
        protective_order_ids=("PROT-1",),
        opened_at=NOW,
    )
    return position, intent


def _evaluate_twice(
    position: PositionState, intent: TradeIntent, mids: dict[str, str]
) -> ExitEvaluation:
    engine = ExitEngine(discovery_exits=EXITS)
    legs = position.legs
    snaps = {
        leg.leg_id: _mid_snapshot(leg.contract, mid=mids[leg.leg_id]) for leg in legs
    }
    first_snap = snaps[legs[0].leg_id]
    first = engine.evaluate(
        position, first_snap, intent, leg_snapshots=snaps, now=CYCLE
    )
    assert first.updated_policy is not None
    again = position.model_copy(update={"exit_policy": first.updated_policy})
    return engine.evaluate(again, first_snap, intent, leg_snapshots=snaps, now=CYCLE)


def test_multi_leg_legs_carry_no_per_leg_prices() -> None:
    for leg in apply_discovery_leg_exit_prices(_bull_put(), EXITS):
        assert leg.current_stop_price is None
        assert leg.current_target_price is None


def test_credit_bounds_target_within_credit_and_stop_below_max_loss() -> None:
    credit = Decimal("44.25") * QTY  # 2876.25
    max_loss = Decimal(200) * QTY - credit  # 10123.75
    pnl_stop, pnl_target = structure_pnl_bounds(
        _bull_put(), EXITS, max_loss=Money.of(str(max_loss), Currency.INR)
    )
    assert pnl_target == Money.of("1725.75", Currency.INR)  # 60% of credit
    assert pnl_target.amount <= credit
    assert pnl_stop == Money.of("-2876.25", Currency.INR)  # loss = 1x credit
    assert 0 < -pnl_stop.amount < max_loss


def test_credit_stop_capped_below_max_loss_for_narrow_spread() -> None:
    legs = (
        _leg("leg-long", Side.BUY, "22350", "10.00", OptionType.PUT),
        _leg("leg-short", Side.SELL, "22400", "40.00", OptionType.PUT),
    )
    # credit 30 on a 50-wide spread: 1x credit (30) would exceed max loss (20).
    pnl_stop, _ = structure_pnl_bounds(legs, EXITS)
    assert pnl_stop == Money.of(str(Decimal("-0.90") * 20 * QTY), Currency.INR)


def test_credit_target_fraction_configurable() -> None:
    config = DiscoveryExitsConfig.model_validate(
        {**EXITS.model_dump(), "credit_target_fraction": "0.50"}
    )
    _, pnl_target = structure_pnl_bounds(_bull_put(), config)
    assert pnl_target == Money.of("1438.12", Currency.INR)


def test_in_profit_credit_spread_hedge_leg_drop_does_not_exit() -> None:
    position, intent = _open(_bull_put(), "10123.75", "TRD-HEDGE")
    # Long hedge 67.45 -> 40.45 (-40%), short 111.70 -> 70.00: P&L +955.50.
    result = _evaluate_twice(
        position, intent, {"leg-long": "40.45", "leg-short": "70.00"}
    )
    assert result.should_exit is False
    assert result.kind is ExitKind.NONE


def test_straddle_one_leg_down_35pct_combined_above_stop_does_not_exit() -> None:
    position, intent = _open(_straddle(), "21794.50", "TRD-STRAD")
    # Call 154.65 -> 100.00 (-35.3%), put 180.65 -> 240.00: P&L +305.50.
    result = _evaluate_twice(
        position, intent, {"leg-call": "100.00", "leg-put": "240.00"}
    )
    assert result.should_exit is False


def test_structure_stop_fires_on_combined_pnl() -> None:
    position, intent = _open(_straddle(), "21794.50", "TRD-STOP")
    # Combined -8144.50 <= stop -7628.08 (35% of 335.30 x 65 debit).
    result = _evaluate_twice(
        position, intent, {"leg-call": "110.00", "leg-put": "100.00"}
    )
    assert result.should_exit is True
    assert result.kind is ExitKind.STOP
    assert result.detail == "strategy P&L stop breached"


def test_credit_structure_target_fires_on_combined_pnl() -> None:
    position, intent = _open(_bull_put(), "10123.75", "TRD-TGT")
    # Combined +2226.25 >= 60% credit target 1725.75.
    result = _evaluate_twice(
        position, intent, {"leg-long": "30.00", "leg-short": "40.00"}
    )
    assert result.should_exit is True
    assert result.kind is ExitKind.TARGET
    assert result.detail == "strategy P&L target reached"


def test_long_leg_rally_on_losing_credit_spread_is_a_stop_not_target() -> None:
    """TRD-...10519 pattern: long put +80% while the spread loses."""
    position, intent = _open(_bull_put(), "10123.75", "TRD-LOSS")
    # Long 67.45 -> 125 (+85%), short 111.70 -> 200: combined -2004.20 (no exit yet).
    result = _evaluate_twice(
        position, intent, {"leg-long": "125.00", "leg-short": "200.00"}
    )
    assert result.kind is not ExitKind.TARGET


def test_single_leg_discovery_unchanged() -> None:
    leg = _leg("leg-1", Side.BUY, "22600", "100.00", OptionType.CALL)
    (stamped,) = apply_discovery_leg_exit_prices((leg,), EXITS)
    assert stamped.current_stop_price == f.price("65.00")
    assert stamped.current_target_price == f.price("180.00")
    pnl_stop, pnl_target = structure_pnl_bounds((leg,), EXITS)
    assert pnl_stop == Money.of("-2275.00", Currency.INR)
    assert pnl_target == Money.of("5200.00", Currency.INR)


def test_structure_exit_closes_all_legs_together(tmp_path: Path) -> None:
    clock = FrozenClock(CYCLE)
    store = TradingStore.open(tmp_path / "paper.sqlite", clock=clock)
    try:
        ids = SequentialIdFactory(clock.instant)
        runner = PaperRunner(
            account_config=_paper_config(),  # type: ignore[arg-type]
            risk_policy=load_risk_policy(ROOT / "config" / "risk.yaml"),
            store=store,
            broker=PaperBroker.from_fixtures(
                BROKER_FIXTURES, clock=clock, id_factory=ids
            ),
            clock=clock,
            id_factory=ids,
            discovery_config=DISCOVERY,
        )
        position, intent = _open(_straddle(), "21794.50", "TRD-ALL")
        mids = {"leg-call": "110.00", "leg-put": "100.00"}
        verdict = _evaluate_twice(position, intent, mids)
        assert verdict.should_exit is True
        assert verdict.detail == "strategy P&L stop breached"
        legs = position.legs
        snaps = {
            leg.contract.symbol: _mid_snapshot(leg.contract, mid=mids[leg.leg_id])
            for leg in legs
        }
        plan = runner._exit_plan(
            intent,
            f.risk_decision(intent_id=intent.intent_id),
            position,
            snaps,
        )
        assert plan is not None
        assert {order.leg_id for order in plan.orders} == {leg.leg_id for leg in legs}
        assert all(order.command.side is Side.SELL for order in plan.orders)
    finally:
        store.close()


def test_discovery_yaml_credit_target_is_sixty_percent() -> None:
    assert DISCOVERY.exits.credit_target_fraction == Decimal("0.60")
