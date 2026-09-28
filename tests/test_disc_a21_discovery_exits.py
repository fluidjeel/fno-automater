"""DISC-A21: DISCOVERY per-leg stop/target, trail, confirm, stale refresh."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

import tests.factories as f
from tests.test_paper_runner import BROKER_FIXTURES, ROOT, _paper_config
from trading.broker.paper import PaperBroker
from trading.config import load_risk_policy
from trading.config.discovery import load_discovery_config
from trading.domain.clock import FrozenClock
from trading.domain.contracts import (
    DerivativesContext,
    FeatureSnapshot,
    IntentLeg,
    PositionLegState,
    PositionLifecycleRecord,
    TradeIntent,
)
from trading.domain.contracts.order_plan import PlannedOrder
from trading.domain.contracts.snapshot import MarketQuote
from trading.domain.enums import (
    ExitScope,
    HoldingStyle,
    OptionType,
    OrderPlanState,
    Side,
    TradeState,
)
from trading.domain.ids import SequentialIdFactory
from trading.domain.primitives import Currency, Money, Price, TickSize
from trading.runtime.paper_runner import PaperRunner, _liability_first
from trading.storage.trading_store import TradingStore
from trading.trade.discovery_exits import (
    apply_discovery_leg_exit_prices,
    apply_discovery_trailing,
    build_discovery_exit_policy,
    compute_leg_stop_price,
    compute_leg_target_price,
    structure_net_debit_per_unit,
    structure_pnl_bounds,
)
from trading.trade.exits import ExitEngine, ExitKind, build_exit_policy

NOW = f.NOW
CYCLE = NOW + timedelta(seconds=60)
DISCOVERY = load_discovery_config(ROOT / "config" / "discovery.yaml").config
EXITS = DISCOVERY.exits
QTY = 65


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(CYCLE)


@pytest.fixture
def store(tmp_path: Path, clock: FrozenClock) -> Iterator[TradingStore]:
    trading_store = TradingStore.open(tmp_path / "paper.sqlite", clock=clock)
    yield trading_store
    trading_store.close()


def _long_leg(
    *,
    leg_id: str,
    symbol: str,
    entry: str,
    strike: str = "22800",
    option_type: OptionType = OptionType.PUT,
) -> PositionLegState:
    contract = f.option_contract(
        symbol=symbol,
        strike=Decimal(strike),
        option_type=option_type,
    )
    return PositionLegState(
        leg_id=leg_id,
        contract=contract,
        side=Side.BUY,
        quantity_contracts=QTY,
        average_entry_price=f.price(entry),
    )


def _strangle_legs(
    *, put_entry: str = "137.25", call_entry: str = "202.75"
) -> tuple[PositionLegState, ...]:
    return (
        _long_leg(
            leg_id="put",
            symbol="NIFTY26SEP22800PE",
            entry=put_entry,
            strike="22800",
            option_type=OptionType.PUT,
        ),
        _long_leg(
            leg_id="call",
            symbol="NIFTY26SEP22900CE",
            entry=call_entry,
            strike="22900",
            option_type=OptionType.CALL,
        ),
    )


def _strangle_intent(legs: tuple[PositionLegState, ...]) -> TradeIntent:
    return f.intent(
        legs=(
            IntentLeg(leg_id="put", contract=legs[0].contract, side=Side.BUY, ratio=1),
            IntentLeg(leg_id="call", contract=legs[1].contract, side=Side.BUY, ratio=1),
        ),
        estimated_max_loss=Money.of("50000", Currency.INR),
    )


def _mid_snapshot(
    contract: object, *, mid: str, now: object = CYCLE
) -> FeatureSnapshot:
    tick = TickSize.of("0.05")
    half = Decimal("0.025")
    value = Decimal(mid)
    bid = Price.snap(value - half, tick)
    ask = Price.snap(value + half, tick)
    return f.snapshot(
        contract=contract,
        market=f.quote(bid=bid, ask=ask, last=Price.snap(value, tick)),
        times=f.snapshot_times(
            event_time=now,
            source_time=now,
            receive_time=now,
            calculation_time=now,
        ),
        derivatives=DerivativesContext(
            days_to_expiry=7,
            open_interest=5000,
            option_type=OptionType.CALL,
            underlying_price=f.price("24000"),
        ),
    )


class TestDiscoveryLegPrices:
    def test_strangle_per_leg_stops_and_targets(self) -> None:
        """Oracle 2026-09-28 long strangle leg prices from each entry."""
        legs = apply_discovery_leg_exit_prices(_strangle_legs(), EXITS)
        put, call = legs
        assert put.current_stop_price == f.price("89.20")
        assert put.current_target_price == f.price("247.05")
        assert call.current_stop_price == f.price("131.75")
        assert call.current_target_price == f.price("364.95")
        assert put.current_stop_price != call.current_stop_price
        assert put.current_target_price != call.current_target_price

    def test_straddle_structure_pnl_bounds(self) -> None:
        """Long straddle ~22950: CE 176.05 + PE 200.65 per unit."""
        legs = (
            _long_leg(
                leg_id="call",
                symbol="NIFTY26SEP22950CE",
                entry="176.05",
                strike="22950",
            ),
            _long_leg(
                leg_id="put",
                symbol="NIFTY26SEP22950PE",
                entry="200.65",
                strike="22950",
                option_type=OptionType.PUT,
            ),
        )
        pnl_stop, pnl_target = structure_pnl_bounds(legs, EXITS)
        assert pnl_stop == Money.of("-8569.92", Currency.INR)
        assert pnl_target == Money.of("19588.40", Currency.INR)

    def test_strangle_structure_pnl_matches_leg_fractions(self) -> None:
        legs = _strangle_legs()
        pnl_stop, pnl_target = structure_pnl_bounds(legs, EXITS)
        assert pnl_stop == Money.of("-7735.00", Currency.INR)
        assert pnl_target == Money.of("17680.00", Currency.INR)

    def test_credit_short_leg_prices_and_structure_pnl(self) -> None:
        short = PositionLegState(
            leg_id="short",
            contract=f.option_contract(symbol="NIFTY26SEP24000CE"),
            side=Side.SELL,
            quantity_contracts=QTY,
            average_entry_price=f.price("50.00"),
        )
        stop = compute_leg_stop_price(
            entry_price=short.average_entry_price, side=Side.SELL, config=EXITS
        )
        target = compute_leg_target_price(
            entry_price=short.average_entry_price, side=Side.SELL, config=EXITS
        )
        assert stop == f.price("100.00")
        assert target == f.price("25.00")
        pnl_stop, pnl_target = structure_pnl_bounds((short,), EXITS)
        assert pnl_stop == Money.of("-3250.00", Currency.INR)
        assert pnl_target == Money.of("1625.00", Currency.INR)

    def test_no_leg_copies_strategy_stop_price(self) -> None:
        legs = apply_discovery_leg_exit_prices(_strangle_legs(), EXITS)
        policy = build_discovery_exit_policy(
            _strangle_intent(legs),
            legs,
            f.exit_template(stop_distance_ticks=40),
            trade_id="TRD-NOCOPY",
            policy_id="EXIT-NOCOPY",
            initialized_at=NOW,
            config=EXITS,
            scope=ExitScope.STRATEGY_PNL,
        )
        assert policy.stop_price is None
        assert all(
            leg.current_stop_price != policy.stop_price
            for leg in legs
            if leg.current_stop_price
        )
        assert len({leg.current_stop_price for leg in legs}) == 2

    def test_strict_tick_policy_unchanged(self) -> None:
        """STRICT keeps 40-tick policy (Rs 130 stop on qty 65)."""
        policy = build_exit_policy(
            f.exit_template(stop_distance_ticks=40),
            trade_id="TRD-STRICT",
            policy_id="EXIT-STRICT",
            entry_price=f.price("100.00"),
            initialized_at=NOW,
            scope=ExitScope.STRATEGY_PNL,
            quantity_contracts=QTY,
        )
        assert policy.premium_scaled is False
        assert policy.pnl_stop == Money.of("-130.00", Currency.INR)


class TestDiscoveryTrailing:
    def test_trail_activates_at_thirty_percent_debit(self) -> None:
        legs = apply_discovery_leg_exit_prices(_strangle_legs(), EXITS)
        policy = build_discovery_exit_policy(
            _strangle_intent(legs),
            legs,
            f.exit_template(stop_distance_ticks=40),
            trade_id="TRD-TRAIL",
            policy_id="EXIT-TRAIL",
            initialized_at=NOW,
            config=EXITS,
            scope=ExitScope.STRATEGY_PNL,
        )
        basis = abs(structure_net_debit_per_unit(legs))
        activation = basis * QTY * EXITS.trail_activate_fraction
        below = Money(
            (activation - Decimal("1")).quantize(Decimal("0.01")), Currency.INR
        )
        inactive = apply_discovery_trailing(
            policy,
            pnl=below,
            structure_basis=basis,
            quantity_contracts=QTY,
            config=EXITS,
        )
        assert inactive.pnl_trail_stop is None
        at = Money(activation.quantize(Decimal("0.01")), Currency.INR)
        active = apply_discovery_trailing(
            policy,
            pnl=at,
            structure_basis=basis,
            quantity_contracts=QTY,
            config=EXITS,
        )
        assert active.pnl_trail_stop is not None
        assert active.strategy_pnl_hwm == at

    def test_trail_tightens_only(self) -> None:
        legs = apply_discovery_leg_exit_prices(_strangle_legs(), EXITS)
        policy = build_discovery_exit_policy(
            _strangle_intent(legs),
            legs,
            f.exit_template(stop_distance_ticks=40),
            trade_id="TRD-TIGHT",
            policy_id="EXIT-TIGHT",
            initialized_at=NOW,
            config=EXITS,
            scope=ExitScope.STRATEGY_PNL,
        )
        basis = abs(structure_net_debit_per_unit(legs))
        peak = Money.of("12000.00", Currency.INR)
        tightened = apply_discovery_trailing(
            policy,
            pnl=peak,
            structure_basis=basis,
            quantity_contracts=QTY,
            config=EXITS,
        )
        assert tightened.pnl_trail_stop == Money.of("6000.00", Currency.INR)
        pullback = Money.of("8000.00", Currency.INR)
        loosen_attempt = apply_discovery_trailing(
            tightened,
            pnl=pullback,
            structure_basis=basis,
            quantity_contracts=QTY,
            config=EXITS,
        )
        assert loosen_attempt.pnl_trail_stop == tightened.pnl_trail_stop

    def test_trail_never_below_initial_pnl_stop(self) -> None:
        legs = apply_discovery_leg_exit_prices(_strangle_legs(), EXITS)
        policy = build_discovery_exit_policy(
            _strangle_intent(legs),
            legs,
            f.exit_template(stop_distance_ticks=40),
            trade_id="TRD-FLOOR",
            policy_id="EXIT-FLOOR",
            initialized_at=NOW,
            config=EXITS,
            scope=ExitScope.STRATEGY_PNL,
        )
        basis = abs(structure_net_debit_per_unit(legs))
        activation = basis * QTY * EXITS.trail_activate_fraction
        at_peak = Money(activation.quantize(Decimal("0.01")), Currency.INR)
        trailed = apply_discovery_trailing(
            policy,
            pnl=at_peak,
            structure_basis=basis,
            quantity_contracts=QTY,
            config=EXITS,
        )
        assert trailed.pnl_trail_stop is not None
        assert policy.pnl_stop is not None
        assert trailed.pnl_trail_stop.amount >= policy.pnl_stop.amount

    def test_hwm_trail_persists_across_restart(self, clock: FrozenClock) -> None:
        legs = apply_discovery_leg_exit_prices(_strangle_legs(), EXITS)
        intent = _strangle_intent(legs)
        policy = build_discovery_exit_policy(
            intent,
            legs,
            f.exit_template(stop_distance_ticks=40),
            trade_id="TRD-HWM",
            policy_id="EXIT-HWM",
            initialized_at=NOW,
            config=EXITS,
            scope=ExitScope.STRATEGY_PNL,
        )
        position = f.position_state(
            trade_id="TRD-HWM",
            state=TradeState.OPEN,
            legs=legs,
            exit_policy=policy,
            protective_order_ids=("PROT-1",),
            opened_at=NOW,
        )
        engine = ExitEngine(discovery_exits=EXITS)
        put_snap = _mid_snapshot(legs[0].contract, mid="190.00")
        call_snap = _mid_snapshot(legs[1].contract, mid="255.00")
        leg_snaps = {"put": put_snap, "call": call_snap}
        first = engine.evaluate(
            position, put_snap, intent, leg_snapshots=leg_snaps, now=CYCLE
        )
        assert first.updated_policy is not None
        assert first.updated_policy.strategy_pnl_hwm is not None
        restored = position.model_copy(
            update={"exit_policy": first.updated_policy, "as_of": CYCLE}
        )
        second = engine.evaluate(
            restored, put_snap, intent, leg_snapshots=leg_snaps, now=CYCLE
        )
        assert second.updated_policy is not None
        assert (
            second.updated_policy.strategy_pnl_hwm
            == first.updated_policy.strategy_pnl_hwm
        )

    def test_giveback_trail_exit(self, clock: FrozenClock) -> None:
        legs = apply_discovery_leg_exit_prices(_strangle_legs(), EXITS)
        intent = _strangle_intent(legs)
        policy = build_discovery_exit_policy(
            intent,
            legs,
            f.exit_template(stop_distance_ticks=40),
            trade_id="TRD-GIVE",
            policy_id="EXIT-GIVE",
            initialized_at=NOW,
            config=EXITS,
            scope=ExitScope.STRATEGY_PNL,
        )
        position = f.position_state(
            trade_id="TRD-GIVE",
            state=TradeState.OPEN,
            legs=legs,
            exit_policy=policy,
            protective_order_ids=("PROT-1",),
            opened_at=NOW,
        )
        engine = ExitEngine(discovery_exits=EXITS)
        peak_put = _mid_snapshot(legs[0].contract, mid="190.00")
        peak_call = _mid_snapshot(legs[1].contract, mid="255.00")
        leg_snaps = {"put": peak_put, "call": peak_call}
        peak_eval = engine.evaluate(
            position, peak_put, intent, leg_snapshots=leg_snaps, now=CYCLE
        )
        assert peak_eval.updated_policy is not None
        assert peak_eval.updated_policy.pnl_trail_stop is not None
        trailed = position.model_copy(
            update={"exit_policy": peak_eval.updated_policy, "as_of": CYCLE}
        )
        giveback_put = _mid_snapshot(legs[0].contract, mid="150.00")
        giveback_call = _mid_snapshot(legs[1].contract, mid="210.00")
        give_snaps = {"put": giveback_put, "call": giveback_call}
        first = engine.evaluate(
            trailed, giveback_put, intent, leg_snapshots=give_snaps, now=CYCLE
        )
        assert first.should_exit is False
        assert first.updated_policy is not None
        assert first.updated_policy.exit_confirm_count == 1
        mid = trailed.model_copy(update={"exit_policy": first.updated_policy})
        second = engine.evaluate(
            mid, giveback_put, intent, leg_snapshots=give_snaps, now=CYCLE
        )
        assert second.should_exit is True
        assert second.kind is ExitKind.STOP
        assert "trail" in second.detail


class TestDiscoveryExitEvaluation:
    def test_two_quote_confirmation_before_exit(self, clock: FrozenClock) -> None:
        legs = apply_discovery_leg_exit_prices(_strangle_legs(), EXITS)
        intent = _strangle_intent(legs)
        policy = build_discovery_exit_policy(
            intent,
            legs,
            f.exit_template(stop_distance_ticks=40),
            trade_id="TRD-CONF",
            policy_id="EXIT-CONF",
            initialized_at=NOW,
            config=EXITS,
            scope=ExitScope.STRATEGY_PNL,
        )
        position = f.position_state(
            trade_id="TRD-CONF",
            state=TradeState.OPEN,
            legs=legs,
            exit_policy=policy,
            protective_order_ids=("PROT-1",),
            opened_at=NOW,
        )
        engine = ExitEngine(discovery_exits=EXITS)
        put_snap = _mid_snapshot(legs[0].contract, mid="80.00")
        call_snap = _mid_snapshot(legs[1].contract, mid="120.00")
        leg_snaps = {"put": put_snap, "call": call_snap}
        first = engine.evaluate(
            position, put_snap, intent, leg_snapshots=leg_snaps, now=CYCLE
        )
        assert first.should_exit is False
        assert first.updated_policy is not None
        assert first.updated_policy.exit_confirm_count == 1
        mid = position.model_copy(update={"exit_policy": first.updated_policy})
        second = engine.evaluate(
            mid, put_snap, intent, leg_snapshots=leg_snaps, now=CYCLE
        )
        assert second.should_exit is True
        assert second.kind is ExitKind.STOP


class TestDiscoveryRunnerIntegration:
    def _runner(
        self,
        store: TradingStore,
        clock: FrozenClock,
        *,
        fetch_calls: list[tuple[str, ...]],
        fetch_quotes: dict[str, MarketQuote] | None = None,
    ) -> PaperRunner:
        ids = SequentialIdFactory(clock.instant)
        broker = PaperBroker.from_fixtures(BROKER_FIXTURES, clock=clock, id_factory=ids)

        def fetch(symbols: tuple[str, ...]) -> dict[str, MarketQuote]:
            fetch_calls.append(symbols)
            quotes: dict[str, MarketQuote] = {}
            for symbol in symbols:
                if fetch_quotes is not None and symbol in fetch_quotes:
                    quotes[symbol] = fetch_quotes[symbol]
                else:
                    quotes[symbol] = f.quote(bid=f.price("90.00"), ask=f.price("90.05"))
            return quotes

        return PaperRunner(
            account_config=_paper_config(),  # type: ignore[arg-type]
            risk_policy=load_risk_policy(ROOT / "config" / "risk.yaml"),
            store=store,
            broker=broker,
            clock=clock,
            id_factory=ids,
            discovery_config=DISCOVERY,
            rest_quote_fetch=fetch,
        )

    def test_stale_leg_rest_refresh_before_evaluate(
        self, store: TradingStore, clock: FrozenClock
    ) -> None:
        fetch_calls: list[tuple[str, ...]] = []
        runner = self._runner(store, clock, fetch_calls=fetch_calls)
        legs = apply_discovery_leg_exit_prices(_strangle_legs(), EXITS)
        intent = _strangle_intent(legs)
        policy = build_discovery_exit_policy(
            intent,
            legs,
            f.exit_template(stop_distance_ticks=40),
            trade_id="TRD-REST",
            policy_id="EXIT-REST",
            initialized_at=NOW,
            config=EXITS,
            scope=ExitScope.STRATEGY_PNL,
        )
        position = f.position_state(
            trade_id="TRD-REST",
            state=TradeState.OPEN,
            legs=legs,
            exit_policy=policy,
            protective_order_ids=("PROT-1",),
            opened_at=NOW,
        )
        record = PositionLifecycleRecord(
            trade_id=position.trade_id,
            position=position,
            intent=intent,
            risk_decision=f.risk_decision(intent_id=intent.intent_id),
            holding_style=HoldingStyle.INTRADAY,
            as_of=position.as_of,
        )
        store.upsert_position_lifecycle(record, event_id="PLC-REST")
        runner.recover_lifecycle()
        stale_time = CYCLE - timedelta(minutes=10)
        stale_put = _mid_snapshot(legs[0].contract, mid="140.00", now=stale_time)
        fresh_call = _mid_snapshot(legs[1].contract, mid="205.00")
        runner.manage_exits(
            {
                legs[0].contract.symbol: stale_put,
                legs[1].contract.symbol: fresh_call,
            }
        )
        assert fetch_calls
        assert legs[0].contract.symbol in fetch_calls[0]

    def test_still_stale_after_rest_skips_exit_with_reason(
        self, store: TradingStore, clock: FrozenClock
    ) -> None:
        stale_time = CYCLE - timedelta(minutes=10)
        fetch_calls: list[tuple[str, ...]] = []
        legs = apply_discovery_leg_exit_prices(_strangle_legs(), EXITS)
        put_symbol = legs[0].contract.symbol
        call_symbol = legs[1].contract.symbol

        def partial_fetch(symbols: tuple[str, ...]) -> dict[str, MarketQuote]:
            fetch_calls.append(symbols)
            # Refresh only the call; put stays stale after the one REST batch.
            return {call_symbol: f.quote(bid=f.price("90.00"), ask=f.price("90.05"))}

        ids = SequentialIdFactory(clock.instant)
        broker = PaperBroker.from_fixtures(BROKER_FIXTURES, clock=clock, id_factory=ids)
        runner = PaperRunner(
            account_config=_paper_config(),  # type: ignore[arg-type]
            risk_policy=load_risk_policy(ROOT / "config" / "risk.yaml"),
            store=store,
            broker=broker,
            clock=clock,
            id_factory=ids,
            discovery_config=DISCOVERY,
            rest_quote_fetch=partial_fetch,
        )
        intent = _strangle_intent(legs)
        policy = build_discovery_exit_policy(
            intent,
            legs,
            f.exit_template(stop_distance_ticks=40),
            trade_id="TRD-STALE",
            policy_id="EXIT-STALE",
            initialized_at=NOW,
            config=EXITS,
            scope=ExitScope.STRATEGY_PNL,
        )
        position = f.position_state(
            trade_id="TRD-STALE",
            state=TradeState.OPEN,
            legs=legs,
            exit_policy=policy,
            protective_order_ids=("PROT-1",),
            opened_at=NOW,
        )
        record = PositionLifecycleRecord(
            trade_id=position.trade_id,
            position=position,
            intent=intent,
            risk_decision=f.risk_decision(intent_id=intent.intent_id),
            holding_style=HoldingStyle.INTRADAY,
            as_of=position.as_of,
        )
        store.upsert_position_lifecycle(record, event_id="PLC-STALE")
        runner.recover_lifecycle()
        crash_put = _mid_snapshot(legs[0].contract, mid="30.00", now=stale_time)
        crash_call = _mid_snapshot(legs[1].contract, mid="30.00", now=stale_time)
        events = runner.manage_exits(
            {
                put_symbol: crash_put,
                call_symbol: crash_call,
            }
        )
        assert fetch_calls
        assert put_symbol in fetch_calls[0]
        assert events == ()
        updated = runner._services.trade_manager.get_position("TRD-STALE")
        assert updated is not None
        assert updated.state is TradeState.OPEN
        assert updated.protection_degraded is True
        assert updated.unprotected_reason is not None
        assert "stale" in updated.unprotected_reason.lower()

    def test_shorts_first_exit_ordering(self) -> None:
        short = PlannedOrder(
            plan_leg_id="plan-short",
            leg_id="short",
            identity=f.order_identity(),
            command=f.order_command(side=Side.BUY),
            plan_state=OrderPlanState.RISK_APPROVED,
        )
        long = PlannedOrder(
            plan_leg_id="plan-long",
            leg_id="long",
            identity=f.order_identity(),
            command=f.order_command(side=Side.SELL),
            plan_state=OrderPlanState.RISK_APPROVED,
        )
        ordered = _liability_first((long, short))
        assert ordered[0].command.side is Side.BUY
        assert ordered[1].command.side is Side.SELL
