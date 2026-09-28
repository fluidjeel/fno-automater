"""DISC-A21: DISCOVERY premium-scaled structure exits."""

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
    EntryProfile,
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
    build_discovery_exit_policy,
    compute_leg_disaster_stop,
    structure_net_debit_per_unit,
)
from trading.trade.exits import ExitEngine, ExitKind, build_exit_policy
from trading.trade.manager import TradeManager

NOW = f.NOW
CYCLE = NOW + timedelta(seconds=60)
DISCOVERY = load_discovery_config(ROOT / "config" / "discovery.yaml").config
QTY = 65


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(CYCLE)


@pytest.fixture
def store(tmp_path: Path, clock: FrozenClock) -> Iterator[TradingStore]:
    trading_store = TradingStore.open(tmp_path / "paper.sqlite", clock=clock)
    yield trading_store
    trading_store.close()


def _discovery_manager(clock: FrozenClock) -> TradeManager:
    ids = SequentialIdFactory(clock.instant)
    return TradeManager(
        clock=clock,
        id_factory=ids,
        discovery_config=DISCOVERY,
        entry_profile=EntryProfile.DISCOVERY,
    )


def _strangle_legs(*, put_entry: str, call_entry: str) -> tuple[PositionLegState, ...]:
    put_contract = f.option_contract(
        symbol="NIFTY26SEP22800PE",
        strike=Decimal("22800"),
        option_type=OptionType.PUT,
    )
    call_contract = f.option_contract(
        symbol="NIFTY26SEP22900CE",
        strike=Decimal("22900"),
    )
    return (
        PositionLegState(
            leg_id="put",
            contract=put_contract,
            side=Side.BUY,
            quantity_contracts=QTY,
            average_entry_price=f.price(put_entry),
            current_stop_price=compute_leg_disaster_stop(
                entry_price=f.price(put_entry),
                side=Side.BUY,
                config=DISCOVERY.exits,
            ),
        ),
        PositionLegState(
            leg_id="call",
            contract=call_contract,
            side=Side.BUY,
            quantity_contracts=QTY,
            average_entry_price=f.price(call_entry),
            current_stop_price=compute_leg_disaster_stop(
                entry_price=f.price(call_entry),
                side=Side.BUY,
                config=DISCOVERY.exits,
            ),
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


class TestDiscoveryPolicyConstruction:
    def test_strangle_debit_340_premium_scaled_stops(self) -> None:
        """Oracle 2026-09-28 strangle: debit ~340/lot → stop ~119×65, target ~272×65."""
        legs = _strangle_legs(put_entry="137.25", call_entry="202.75")
        assert structure_net_debit_per_unit(legs) == Decimal("340.00")
        intent = _strangle_intent(legs)
        policy = build_discovery_exit_policy(
            intent,
            legs,
            f.exit_template(stop_distance_ticks=40),
            trade_id="TRD-STRANGLE",
            policy_id="EXIT-STRANGLE",
            initialized_at=NOW,
            config=DISCOVERY.exits,
            scope=ExitScope.STRATEGY_PNL,
            quantity_contracts=QTY,
        )
        assert policy.premium_scaled is True
        assert policy.pnl_stop == Money.of("-7735.00", Currency.INR)
        assert policy.pnl_target == Money.of("17680.00", Currency.INR)
        assert policy.stop_price is None

    def test_straddle_debit_377_premium_scaled_stops(self) -> None:
        """Oracle 2026-09-28 straddle: combined premium ~377/lot."""
        call = f.price("188.50")
        put = f.price("188.50")
        legs = (
            PositionLegState(
                leg_id="call",
                contract=f.option_contract(symbol="NIFTY26SEP22950CE"),
                side=Side.BUY,
                quantity_contracts=QTY,
                average_entry_price=call,
            ),
            PositionLegState(
                leg_id="put",
                contract=f.option_contract(
                    symbol="NIFTY26SEP22950PE", option_type=OptionType.PUT
                ),
                side=Side.BUY,
                quantity_contracts=QTY,
                average_entry_price=put,
            ),
        )
        assert structure_net_debit_per_unit(legs) == Decimal("377.00")
        policy = build_discovery_exit_policy(
            _strangle_intent(legs),
            legs,
            f.exit_template(stop_distance_ticks=40),
            trade_id="TRD-STRADDLE",
            policy_id="EXIT-STRADDLE",
            initialized_at=NOW,
            config=DISCOVERY.exits,
            scope=ExitScope.STRATEGY_PNL,
            quantity_contracts=QTY,
        )
        assert policy.pnl_stop == Money.of("-8576.75", Currency.INR)
        assert policy.pnl_target == Money.of("19604.00", Currency.INR)

    def test_per_leg_disaster_stops_not_copied(self) -> None:
        """Each leg keeps its own disaster backstop; no monitor-leg stop copy."""
        legs = _strangle_legs(put_entry="135.25", call_entry="202.75")
        put_stop = legs[0].current_stop_price
        call_stop = legs[1].current_stop_price
        assert put_stop is not None and call_stop is not None
        assert put_stop.value == Decimal("40.55")
        assert call_stop.value == Decimal("60.80")
        assert call_stop.value != put_stop.value
        assert call_stop.value != Decimal("135.25")

    def test_strict_tick_policy_unchanged(self) -> None:
        """STRICT keeps 40-tick policy (₹130 stop on qty 65)."""
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


class TestDiscoveryExitEvaluation:
    def test_hwm_trail_persists_across_restart(self, clock: FrozenClock) -> None:
        """Structure P&L HWM trail survives lifecycle restore."""
        legs = _strangle_legs(put_entry="137.25", call_entry="202.75")
        intent = _strangle_intent(legs)
        policy = build_discovery_exit_policy(
            intent,
            legs,
            f.exit_template(stop_distance_ticks=40),
            trade_id="TRD-HWM",
            policy_id="EXIT-HWM",
            initialized_at=NOW,
            config=DISCOVERY.exits,
            scope=ExitScope.STRATEGY_PNL,
            quantity_contracts=QTY,
        )
        position = f.position_state(
            trade_id="TRD-HWM",
            state=TradeState.OPEN,
            legs=legs,
            exit_policy=policy,
            protective_order_ids=("PROT-1",),
            opened_at=NOW,
        )
        engine = ExitEngine(discovery_exits=DISCOVERY.exits)
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

    def test_two_quote_confirmation_before_exit(self, clock: FrozenClock) -> None:
        """Stop requires two consecutive confirming mid quotes."""
        legs = _strangle_legs(put_entry="137.25", call_entry="202.75")
        intent = _strangle_intent(legs)
        policy = build_discovery_exit_policy(
            intent,
            legs,
            f.exit_template(stop_distance_ticks=40),
            trade_id="TRD-CONF",
            policy_id="EXIT-CONF",
            initialized_at=NOW,
            config=DISCOVERY.exits,
            scope=ExitScope.STRATEGY_PNL,
            quantity_contracts=QTY,
        )
        position = f.position_state(
            trade_id="TRD-CONF",
            state=TradeState.OPEN,
            legs=legs,
            exit_policy=policy,
            protective_order_ids=("PROT-1",),
            opened_at=NOW,
        )
        engine = ExitEngine(discovery_exits=DISCOVERY.exits)
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
    ) -> PaperRunner:
        ids = SequentialIdFactory(clock.instant)
        broker = PaperBroker.from_fixtures(BROKER_FIXTURES, clock=clock, id_factory=ids)

        def fetch(symbols: tuple[str, ...]) -> dict[str, MarketQuote]:
            fetch_calls.append(symbols)
            quotes: dict[str, MarketQuote] = {}
            for symbol in symbols:
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
        """All-legs-fresh gate batches REST refresh for stale structure legs."""
        fetch_calls: list[tuple[str, ...]] = []
        runner = self._runner(store, clock, fetch_calls=fetch_calls)
        legs = _strangle_legs(put_entry="137.25", call_entry="202.75")
        intent = _strangle_intent(legs)
        policy = build_discovery_exit_policy(
            intent,
            legs,
            f.exit_template(stop_distance_ticks=40),
            trade_id="TRD-REST",
            policy_id="EXIT-REST",
            initialized_at=NOW,
            config=DISCOVERY.exits,
            scope=ExitScope.STRATEGY_PNL,
            quantity_contracts=QTY,
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

    def test_shorts_first_exit_ordering(self) -> None:
        """Structure exits close short legs before releasing longs."""
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
