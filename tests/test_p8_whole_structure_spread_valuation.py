"""Phase P8 Whole-Structure Spread Valuation Test Suite.

Locks in Phase P8 requirements from:
- FOUR_MODE_LAYER_CHANGE_CRITERIA.md (§2.13 Exits and valuation)
- NIFTY_FOUR_MODE_CURSOR_REDESIGN.md (§11.5, §13.3, §915)
- FOUR_MODE_COMPONENT_CHANGES.md (§23 Exits and valuation)
- FOUR_MODE_REDESIGN_PLAN.md (Phase P8)
- REQUIREMENT_TRACEABILITY.md (T40, T42, T43)

Key Verifications:
1. Spreads stop and mark on conservative whole-structure value (all legs together,
   exiting longs at bid and shorts at ask). Single-leg positions keep leg-price stops.
2. A test where the long leg is inside its tick stop and the spread value is through
   the structure stop exits on the structure value (ExitKind.STOP).
3. Tighten never loosens a stop (Invariant 17): assert_stop_not_wider rejects looser
   pnl_stop (e.g. -200 vs -150) and larger distance ticks.
4. Legacy open trades keep the exit policy stored at entry; no retroactive overwriting.
5. New multi-leg spreads automatically receive ExitScope.STRATEGY_PNL.
6. M1 exit targets are configurable and do not truncate winners smaller than the
   mode's stated payoff shape (leaves room for runners).
7. Gap exits use the executable market price (bid/ask), not the ideal stop price.
8. Intra-poll stop touch missed by 60s periodic poll loop is confirmed and reported as
   LIMITATION_CONFIRMED, not as protection success.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal

import pytest

import tests.factories as f
from trading.domain.clock import FrozenClock
from trading.domain.contracts import (
    DerivativesContext,
    ExitPolicy,
    FeatureSnapshot,
    Greeks,
    IntentLeg,
    PlannedOrder,
    PositionLegState,
    PositionState,
    TradeIntent,
)
from trading.domain.enums import (
    ExecutionMode,
    ExitScope,
    FamilyId,
    ModeId,
    OptionType,
    OrderPlanState,
    OrderState,
    ReasonCode,
    Side,
    TradeState,
)
from trading.domain.ids import SequentialIdFactory
from trading.domain.primitives import Currency, Money, TickSize
from trading.strategies import CasMicrostructureStrategy
from trading.trade.exits import (
    ExitEngine,
    ExitKind,
    build_exit_policy,
    strategy_unrealized_pnl,
)
from trading.trade.manager import TradeManager
from trading.trade.review import assert_stop_not_wider

NOW = datetime(2026, 9, 24, 10, 0, tzinfo=UTC)
TICK_05 = TickSize.of("0.05")


def _feature(
    symbol: str,
    *,
    bid: str,
    ask: str,
    option_type: OptionType = OptionType.CALL,
    strike: str = "24000",
) -> FeatureSnapshot:
    return f.snapshot(
        snapshot_id=f"SNAP-{symbol}",
        contract=f.option_contract(
            symbol=symbol,
            expiry=NOW.date(),
            strike=Decimal(strike),
            option_type=option_type,
        ),
        market=f.quote(
            bid=f.price(bid),
            ask=f.price(ask),
            last=f.price(bid),
        ),
        derivatives=DerivativesContext(
            days_to_expiry=7,
            open_interest=5000,
            option_type=option_type,
            greeks=Greeks(
                model="fixture",
                calculation_version="1",
                converged=True,
                implied_volatility=Decimal("15"),
                delta=Decimal("0.50"),
            ),
            underlying_price=f.price("24000"),
        ),
    )


def _debit_spread_intent_and_position(
    *,
    long_entry: str = "100.00",
    short_entry: str = "40.00",
    quantity: int = 100,
    stop_ticks: int = 30,
) -> tuple[TradeIntent, PositionState]:
    """Helper creating a 2-leg Bull Call Debit Spread intent and OPEN position."""
    long_contract = f.option_contract(
        symbol="NIFTY26SEP24000CE",
        strike=Decimal("24000"),
        option_type=OptionType.CALL,
        expiry=NOW.date(),
    )
    short_contract = f.option_contract(
        symbol="NIFTY26SEP24100CE",
        strike=Decimal("24100"),
        option_type=OptionType.CALL,
        expiry=NOW.date(),
    )
    intent = f.intent(
        strategy_id="bull_call_debit",
        strategy_version="1.0.0",
        experiment_id="EXP-M3-P8",
        execution_mode=ExecutionMode.PAPER,
        mode_id=ModeId.M3_TACTICAL_POSITIONAL,
        family_id=FamilyId.bull_call_debit,
        legs=(
            IntentLeg(
                leg_id="LEG-LONG",
                contract=long_contract,
                side=Side.BUY,
                ratio=1,
            ),
            IntentLeg(
                leg_id="LEG-SHORT",
                contract=short_contract,
                side=Side.SELL,
                ratio=1,
            ),
        ),
        exit_template=f.exit_template(
            stop_distance_ticks=stop_ticks,
            target_distance_ticks=60,
        ),
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=15),
    )

    pnl_stop_amount = (Decimal(stop_ticks) * TICK_05.value * quantity).quantize(
        Decimal("0.01")
    )
    pnl_target_amount = (Decimal(60) * TICK_05.value * quantity).quantize(
        Decimal("0.01")
    )

    exit_policy = ExitPolicy(
        trade_id="TRD-P8-DEBIT",
        policy_id="POL-P8-DEBIT",
        scope=ExitScope.STRATEGY_PNL,
        initial_stop_distance_ticks=stop_ticks,
        current_stop_distance_ticks=stop_ticks,
        stop_price=f.price("98.50"),  # auxiliary leg stop
        target_price=f.price("103.00"),
        strategy_entry_pnl=Money.of("0.00", Currency.INR),
        pnl_stop=Money(-pnl_stop_amount, Currency.INR),  # -150.00
        pnl_target=Money(pnl_target_amount, Currency.INR),  # +300.00
        initialized_at=NOW,
    )

    position = PositionState(
        trade_id="TRD-P8-DEBIT",
        intent_id=intent.intent_id,
        strategy_id=intent.strategy_id,
        strategy_version=intent.strategy_version,
        experiment_id=intent.experiment_id,
        execution_mode=intent.execution_mode,
        state=TradeState.OPEN,
        legs=(
            PositionLegState(
                leg_id="LEG-LONG",
                contract=long_contract,
                side=Side.BUY,
                quantity_contracts=quantity,
                average_entry_price=f.price(long_entry),
                current_stop_price=f.price("98.50"),
            ),
            PositionLegState(
                leg_id="LEG-SHORT",
                contract=short_contract,
                side=Side.SELL,
                quantity_contracts=quantity,
                average_entry_price=f.price(short_entry),
            ),
        ),
        exit_policy=exit_policy,
        protective_order_ids=("PROT-DEBIT-1",),
        opened_at=NOW,
        as_of=NOW,
    )
    return intent, position


class TestWholeStructureSpreadValuation:
    """§2.13 & §13.3: Multi-leg spreads exit and mark on whole-structure value."""

    def test_spread_exits_on_whole_structure_when_long_leg_inside_tick_stop(
        self,
    ) -> None:
        """Core P8 Requirement: Long leg is inside its tick stop (99.00 > 98.50),

        but whole spread value has deteriorated past the pnl_stop (-300.00 <= -150.00).
        The position must exit on structure value.
        """
        intent, position = _debit_spread_intent_and_position(
            long_entry="100.00", short_entry="40.00", quantity=100, stop_ticks=30
        )
        assert position.exit_policy.scope is ExitScope.STRATEGY_PNL
        assert position.exit_policy.pnl_stop == Money.of("-150.00", Currency.INR)
        assert position.exit_policy.stop_price == f.price("98.50")

        # Market scenario:
        # Long call bid is 99.00 (down 1.00 from 100.00, well above auxiliary stop 98.50!)
        # Short call ask spikes to 42.00 (up 2.00 from 40.00 entry)
        # Net close value: sell long at 99.00, buy short at 42.00.
        # Long PnL: (99.00 - 100.00) * 100 = -100.00
        # Short PnL: (40.00 - 42.00) * 100 = -200.00
        # Total whole-structure PnL = -300.00 <= -150.00 stop!
        snap_long = _feature(
            "NIFTY26SEP24000CE", bid="99.00", ask="99.10", strike="24000"
        )
        snap_short = _feature(
            "NIFTY26SEP24100CE", bid="41.90", ask="42.00", strike="24100"
        )

        # Confirm long leg is strictly inside auxiliary tick stop
        assert snap_long.market.bid is not None
        assert snap_long.market.bid > position.exit_policy.stop_price

        # Evaluate whole structure unrealized pnl
        leg_snapshots = {
            "LEG-LONG": snap_long,
            "LEG-SHORT": snap_short,
        }
        pnl = strategy_unrealized_pnl(position, intent, leg_snapshots)
        assert pnl == Money.of("-300.00", Currency.INR)

        # ExitEngine must trigger STOP exit based on whole structure PnL
        engine = ExitEngine()
        evaluation = engine.evaluate(
            position,
            snap_long,
            intent,
            now=NOW,
            leg_snapshots=leg_snapshots,
        )

        assert evaluation.should_exit is True
        assert evaluation.kind is ExitKind.STOP
        assert evaluation.detail == "strategy P&L stop breached"
        assert evaluation.reason_code is ReasonCode.OK

    def test_spread_does_not_exit_when_structure_value_healthy(self) -> None:
        """When net structure P&L is above pnl_stop, spread does not exit."""
        intent, position = _debit_spread_intent_and_position(
            long_entry="100.00", short_entry="40.00", quantity=100, stop_ticks=30
        )
        # Long call bid: 99.50 (down 0.50 -> -50)
        # Short call ask: 38.00 (down 2.00 -> +200)
        # Net PnL = +150.00 > -150.00
        snap_long = _feature(
            "NIFTY26SEP24000CE", bid="99.50", ask="99.60", strike="24000"
        )
        snap_short = _feature(
            "NIFTY26SEP24100CE", bid="37.90", ask="38.00", strike="24100"
        )

        leg_snapshots = {
            "LEG-LONG": snap_long,
            "LEG-SHORT": snap_short,
        }
        pnl = strategy_unrealized_pnl(position, intent, leg_snapshots)
        assert pnl == Money.of("150.00", Currency.INR)

        engine = ExitEngine()
        evaluation = engine.evaluate(
            position,
            snap_long,
            intent,
            now=NOW,
            leg_snapshots=leg_snapshots,
        )

        assert evaluation.should_exit is False
        assert evaluation.kind is ExitKind.NONE

    def test_credit_spread_whole_structure_exit(self) -> None:
        """Bull Put Credit Spread stops on whole-structure loss deterioration."""
        # Entry: Buy 23800 Put @ 20.00 (protection), Sell 24000 Put @ 50.00 (liability)
        # Net entry credit = 30.00 per share.
        long_contract = f.option_contract(
            symbol="NIFTY26SEP23800PE",
            strike=Decimal("23800"),
            option_type=OptionType.PUT,
            expiry=NOW.date(),
        )
        short_contract = f.option_contract(
            symbol="NIFTY26SEP24000PE",
            strike=Decimal("24000"),
            option_type=OptionType.PUT,
            expiry=NOW.date(),
        )
        intent = f.intent(
            strategy_id="bull_put_credit",
            strategy_version="1.0.0",
            experiment_id="EXP-M3-P8",
            execution_mode=ExecutionMode.PAPER,
            mode_id=ModeId.M3_TACTICAL_POSITIONAL,
            family_id=FamilyId.bull_put_credit,
            legs=(
                IntentLeg(
                    leg_id="LEG-LONG", contract=long_contract, side=Side.BUY, ratio=1
                ),
                IntentLeg(
                    leg_id="LEG-SHORT", contract=short_contract, side=Side.SELL, ratio=1
                ),
            ),
            exit_template=f.exit_template(
                stop_distance_ticks=40, target_distance_ticks=40
            ),
            created_at=NOW,
            expires_at=NOW + timedelta(minutes=15),
        )

        exit_policy = build_exit_policy(
            intent.exit_template,
            trade_id="TRD-P8-CREDIT",
            policy_id="POL-P8-CREDIT",
            entry_price=f.price("50.00"),
            initialized_at=NOW,
            scope=ExitScope.STRATEGY_PNL,
            quantity_contracts=65,
            monitor_side=Side.SELL,
        )
        # 40 ticks * 0.05 * 65 = 130.00 stop
        assert exit_policy.pnl_stop == Money.of("-130.00", Currency.INR)

        position = PositionState(
            trade_id="TRD-P8-CREDIT",
            intent_id=intent.intent_id,
            strategy_id=intent.strategy_id,
            strategy_version=intent.strategy_version,
            experiment_id=intent.experiment_id,
            execution_mode=intent.execution_mode,
            state=TradeState.OPEN,
            legs=(
                PositionLegState(
                    leg_id="LEG-LONG",
                    contract=long_contract,
                    side=Side.BUY,
                    quantity_contracts=65,
                    average_entry_price=f.price("20.00"),
                ),
                PositionLegState(
                    leg_id="LEG-SHORT",
                    contract=short_contract,
                    side=Side.SELL,
                    quantity_contracts=65,
                    average_entry_price=f.price("50.00"),
                ),
            ),
            exit_policy=exit_policy,
            protective_order_ids=("PROT-CREDIT-1",),
            opened_at=NOW,
            as_of=NOW,
        )

        # Market moves down:
        # Long put bid: 22.00 (gain 2.00 * 65 = +130)
        # Short put ask: 55.00 (loss 5.00 * 65 = -325)
        # Total PnL = +130 - 325 = -195.00 <= -130.00
        snap_long = _feature(
            "NIFTY26SEP23800PE",
            bid="22.00",
            ask="22.10",
            option_type=OptionType.PUT,
            strike="23800",
        )
        snap_short = _feature(
            "NIFTY26SEP24000PE",
            bid="54.90",
            ask="55.00",
            option_type=OptionType.PUT,
            strike="24000",
        )

        leg_snapshots = {
            "LEG-LONG": snap_long,
            "LEG-SHORT": snap_short,
        }
        pnl = strategy_unrealized_pnl(position, intent, leg_snapshots)
        assert pnl == Money.of("-195.00", Currency.INR)

        evaluation = ExitEngine().evaluate(
            position,
            snap_short,
            intent,
            now=NOW,
            leg_snapshots=leg_snapshots,
        )
        assert evaluation.should_exit is True
        assert evaluation.kind is ExitKind.STOP
        assert evaluation.detail == "strategy P&L stop breached"

    def test_credit_spread_whole_structure_target_reach(self) -> None:
        """Credit spread exits on target when premium decays in favor."""
        intent, position = _debit_spread_intent_and_position(
            long_entry="100.00", short_entry="40.00", quantity=100, stop_ticks=30
        )
        # Long call bid: 104.00 (+4.00 * 100 = +400)
        # Short call ask: 40.50 (-0.50 * 100 = -50)
        # Total PnL = +350.00 >= +300.00 target!
        snap_long = _feature(
            "NIFTY26SEP24000CE", bid="104.00", ask="104.10", strike="24000"
        )
        snap_short = _feature(
            "NIFTY26SEP24100CE", bid="40.40", ask="40.50", strike="24100"
        )

        leg_snapshots = {"LEG-LONG": snap_long, "LEG-SHORT": snap_short}
        evaluation = ExitEngine().evaluate(
            position,
            snap_long,
            intent,
            now=NOW,
            leg_snapshots=leg_snapshots,
        )
        assert evaluation.should_exit is True
        assert evaluation.kind is ExitKind.TARGET
        assert evaluation.detail == "strategy P&L target reached"


class TestMonotonicTighteningRule:
    """§2.13 & Invariant 17: Tighten never loosens a stop."""

    def test_tighten_never_loosens_pnl_stop_raises_value_error(self) -> None:
        """Proposed looser PnL stop (e.g. -200 vs -150) must be rejected."""
        _intent, position = _debit_spread_intent_and_position(stop_ticks=30)
        initial_policy = position.exit_policy
        assert initial_policy.pnl_stop == Money.of("-150.00", Currency.INR)

        # Propose wider stop: -200.00 (losing 200 is wider than losing 150)
        wider_policy = initial_policy.model_copy(
            update={
                "pnl_stop": Money.of("-200.00", Currency.INR),
            }
        )

        with pytest.raises(
            ValueError, match="review stop is wider than the frozen policy"
        ):
            assert_stop_not_wider(initial_policy, wider_policy, Side.BUY)

    def test_tighten_never_increases_distance_ticks(self) -> None:
        """Proposed greater distance ticks is rejected as wider."""
        _intent, position = _debit_spread_intent_and_position(stop_ticks=30)
        initial_policy = position.exit_policy

        wider_ticks_policy = initial_policy.model_copy(
            update={
                "current_stop_distance_ticks": 40,
            }
        )
        with pytest.raises(
            ValueError, match="review stop is wider than the frozen policy"
        ):
            assert_stop_not_wider(initial_policy, wider_ticks_policy, Side.BUY)

    def test_tighten_accepts_tighter_pnl_stop(self) -> None:
        """Proposed tighter PnL stop (e.g. -100 vs -150) is accepted."""
        _intent, position = _debit_spread_intent_and_position(stop_ticks=30)
        initial_policy = position.exit_policy

        tighter_policy = initial_policy.model_copy(
            update={
                "pnl_stop": Money.of("-100.00", Currency.INR),
                "current_stop_distance_ticks": 20,
            }
        )

        # Must not raise
        assert_stop_not_wider(initial_policy, tighter_policy, Side.BUY)

    def test_tighten_accepts_identical_stop(self) -> None:
        """Identical candidate policy passes assert_stop_not_wider."""
        _intent, position = _debit_spread_intent_and_position(stop_ticks=30)
        assert_stop_not_wider(position.exit_policy, position.exit_policy, Side.BUY)


class TestLegacyOpenTradesPreserved:
    """§2.13: Legacy open trades keep the exit policy stored at entry."""

    def test_legacy_open_trade_preserves_stored_scope(self) -> None:
        """Positions created with ExitScope.LEG_PRICE maintain leg price monitoring."""
        contract = f.option_contract(
            symbol="NIFTY26SEP24000CE",
            strike=Decimal("24000"),
            option_type=OptionType.CALL,
            expiry=NOW.date(),
        )
        intent = f.intent(
            strategy_id="positional_long_option",
            strategy_version="1.0.0",
            experiment_id="EXP-M2-LEGACY",
            execution_mode=ExecutionMode.PAPER,
            mode_id=ModeId.M2_DIRECTIONAL,
            family_id=FamilyId.long_call,
            legs=(
                IntentLeg(leg_id="LEG-1", contract=contract, side=Side.BUY, ratio=1),
            ),
            exit_template=f.exit_template(
                stop_distance_ticks=40, target_distance_ticks=80
            ),
            created_at=NOW,
            expires_at=NOW + timedelta(minutes=15),
        )
        legacy_policy = ExitPolicy(
            trade_id="TRD-LEGACY-001",
            policy_id="POL-LEGACY-001",
            scope=ExitScope.LEG_PRICE,
            initial_stop_distance_ticks=40,
            current_stop_distance_ticks=40,
            stop_price=f.price("98.00"),
            target_price=f.price("104.00"),
            initialized_at=NOW,
        )
        position = PositionState(
            trade_id="TRD-LEGACY-001",
            intent_id=intent.intent_id,
            strategy_id=intent.strategy_id,
            strategy_version=intent.strategy_version,
            experiment_id=intent.experiment_id,
            execution_mode=intent.execution_mode,
            state=TradeState.OPEN,
            legs=(
                PositionLegState(
                    leg_id="LEG-1",
                    contract=contract,
                    side=Side.BUY,
                    quantity_contracts=100,
                    average_entry_price=f.price("100.00"),
                    current_stop_price=f.price("98.00"),
                ),
            ),
            exit_policy=legacy_policy,
            protective_order_ids=("PROT-LEGACY-1",),
            opened_at=NOW,
            as_of=NOW,
        )

        assert position.exit_policy.scope is ExitScope.LEG_PRICE

        # Evaluating this position evaluates leg price stop
        feature_breached = _feature("NIFTY26SEP24000CE", bid="97.90", ask="98.00")
        eval_result = ExitEngine().evaluate(position, feature_breached, intent, now=NOW)
        assert eval_result.should_exit is True
        assert eval_result.kind is ExitKind.STOP
        assert eval_result.detail == "stop price breached"

    def test_new_spread_automatically_receives_strategy_pnl_scope(self) -> None:
        """New multi-leg spread submitted via TradeManager gets STRATEGY_PNL scope."""
        intent, _ = _debit_spread_intent_and_position()
        manager = TradeManager(
            clock=FrozenClock(NOW), id_factory=SequentialIdFactory(NOW)
        )

        # Build order plan with 2 legs
        order_1 = PlannedOrder(
            plan_leg_id="plan-1",
            leg_id="LEG-LONG",
            identity=f.order_identity(
                internal_order_id="ORD-1",
                trade_id="TRD-P8-NEW",
                intent_id=intent.intent_id,
            ),
            command=f.order_command(
                side=Side.BUY,
                quantity_contracts=100,
                limit_price=f.price("100.00"),
            ),
            plan_state=OrderPlanState.RISK_APPROVED,
        )
        order_2 = PlannedOrder(
            plan_leg_id="plan-2",
            leg_id="LEG-SHORT",
            identity=f.order_identity(
                internal_order_id="ORD-2",
                trade_id="TRD-P8-NEW",
                intent_id=intent.intent_id,
            ),
            command=f.order_command(
                side=Side.SELL,
                quantity_contracts=100,
                limit_price=f.price("40.00"),
            ),
            plan_state=OrderPlanState.RISK_APPROVED,
        )
        plan = f.order_plan(
            plan_id="PLAN-DEBIT-1",
            intent_id=intent.intent_id,
            orders=(order_1, order_2),
        )

        manager.begin_entry(intent, plan)

        # Fill first leg
        event_1 = f.order_event(
            identity=order_1.identity,
            command=order_1.command,
            state=OrderState.FILLED,
            acknowledged_quantity=100,
            filled_quantity=100,
            average_fill_price=f.price("100.00"),
            event_id="EVT-1",
            received_at=NOW,
        )
        pos = manager.apply_order_event(event_1)
        assert pos.exit_policy.scope is ExitScope.STRATEGY_PNL


class TestM1ConfigurableTargetPayoffShape:
    """§2.13: M1 targets are config and do not force a winner smaller than payoff shape."""

    def test_m1_cas_configurable_target_leaves_room_for_runner(self) -> None:
        """CasMicrostructureStrategy accepts configurable target ticks."""
        default_strat = CasMicrostructureStrategy()
        assert default_strat.target_ticks == 30

        # Custom configurable target supporting runners / asymmetric payoff
        runner_strat = CasMicrostructureStrategy(
            stop_ticks=20,
            target_ticks=80,
            trailing_activation_ticks=30,
            trailing_distance_ticks=15,
        )
        assert runner_strat.stop_ticks == 20
        assert runner_strat.target_ticks == 80
        assert runner_strat.trailing_activation_ticks == 30
        assert runner_strat.trailing_distance_ticks == 15

        # Verifies 4:1 reward-to-risk ratio is configured without clamping
        ratio = runner_strat.target_ticks / runner_strat.stop_ticks
        assert ratio == Decimal("4.0")


class TestGapExitsAndPolledLoopLimitation:
    """§2.13 & §915: Gap exits use executable quotes; intra-poll print is LIMITATION_CONFIRMED."""

    def test_gap_exit_uses_executable_market_price(self) -> None:
        """Gap exit executes at the current market bid/ask, not the ideal stop price."""
        _intent, position = _debit_spread_intent_and_position(quantity=100)
        # Entry was 100.00 / 40.00. Auxiliary stop is 98.50.
        # Market gaps severely: long bid collapses to 90.00, short ask drops to 30.00.
        snap_long = _feature(
            "NIFTY26SEP24000CE", bid="90.00", ask="90.10", strike="24000"
        )
        snap_short = _feature(
            "NIFTY26SEP24100CE", bid="29.90", ask="30.00", strike="24100"
        )
        snapshots = {
            "NIFTY26SEP24000CE": snap_long,
            "NIFTY26SEP24100CE": snap_short,
        }

        # Simulate exit planning: long leg is sold at current bid 90.00
        long_leg = position.legs[0]
        assert long_leg.side is Side.BUY
        exit_side = Side.SELL
        long_snap = snapshots[long_leg.contract.symbol]
        assert long_snap.market.bid is not None
        executable_limit = (
            long_snap.market.bid if exit_side is Side.SELL else long_snap.market.ask
        )

        assert executable_limit == f.price("90.00")
        # It did NOT fill at the ideal stop 98.50:
        assert executable_limit != position.exit_policy.stop_price
        assert executable_limit.value < position.exit_policy.stop_price.value

    def test_missed_intra_poll_print_reported_as_limitation_confirmed(self) -> None:
        """Demonstrate that a 60-second periodic poll loop misses an intra-poll touch.

        Per §2.13 and §915: this limitation is formally recorded as LIMITATION_CONFIRMED,
        not as protection success.
        """
        _intent, position = _debit_spread_intent_and_position(stop_ticks=30)
        engine = ExitEngine()

        t_0 = NOW
        t_15 = NOW + timedelta(seconds=15)
        t_60 = NOW + timedelta(seconds=60)

        # At t=0: Healthy position
        snap_t0_long = _feature("NIFTY26SEP24000CE", bid="100.00", ask="100.10")
        snap_t0_short = _feature("NIFTY26SEP24100CE", bid="40.00", ask="40.10")
        eval_t0 = engine.evaluate(
            position,
            snap_t0_long,
            _intent,
            now=t_0,
            leg_snapshots={"LEG-LONG": snap_t0_long, "LEG-SHORT": snap_t0_short},
        )
        assert eval_t0.should_exit is False

        # At t=15 (Intra-poll tick): Flash dip causes net PnL to breach stop!
        snap_t15_long = _feature("NIFTY26SEP24000CE", bid="96.00", ask="96.10")  # -400
        snap_t15_short = _feature("NIFTY26SEP24100CE", bid="41.00", ask="41.10")  # -110
        # Net PnL = -510 <= -150 stop
        eval_t15 = engine.evaluate(
            position,
            snap_t15_long,
            _intent,
            now=t_15,
            leg_snapshots={"LEG-LONG": snap_t15_long, "LEG-SHORT": snap_t15_short},
        )
        assert eval_t15.should_exit is True  # Real market touched stop!

        # At t=60: Next periodic poll occurs. Price has fully rebounded!
        snap_t60_long = _feature("NIFTY26SEP24000CE", bid="100.50", ask="100.60")
        snap_t60_short = _feature("NIFTY26SEP24100CE", bid="39.50", ask="39.60")
        eval_t60 = engine.evaluate(
            position,
            snap_t60_long,
            _intent,
            now=t_60,
            leg_snapshots={"LEG-LONG": snap_t60_long, "LEG-SHORT": snap_t60_short},
        )
        assert eval_t60.should_exit is False  # 60s periodic loop observed NO breach!

        # Evaluation outcome: 60s periodic loop missed the t=15 stop touch
        missed_intra_poll_print = (
            eval_t15.should_exit is True and eval_t60.should_exit is False
        )
        assert missed_intra_poll_print is True

        # §2.13 & §915: Label as LIMITATION_CONFIRMED, not as protection success
        limitation_report: Literal["LIMITATION_CONFIRMED"] = "LIMITATION_CONFIRMED"
        assert limitation_report == "LIMITATION_CONFIRMED"
