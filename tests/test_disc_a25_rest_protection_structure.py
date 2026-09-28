"""DISC-A25: REST protection quotes reach structure-leg checks."""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest

import tests.factories as f
from tests.test_disc_a21_discovery_exits import (
    CYCLE,
    DISCOVERY,
    EXITS,
    NOW,
    _mid_snapshot,
    _strangle_intent,
    _strangle_legs,
)
from tests.test_paper_runner import BROKER_FIXTURES, ROOT, _paper_config
from trading.broker.paper import PaperBroker
from trading.config import load_risk_policy
from trading.domain.clock import FrozenClock
from trading.domain.contracts import PositionLegState, PositionLifecycleRecord
from trading.domain.contracts.snapshot import MarketQuote
from trading.domain.enums import (
    ExitScope,
    HoldingStyle,
    OptionType,
    QuoteMonitorSource,
    Side,
    TradeState,
)
from trading.domain.ids import SequentialIdFactory
from trading.domain.primitives import Price, TickSize
from trading.runtime.paper_runner import PaperRunner
from trading.storage.trading_store import TradingStore
from trading.trade.discovery_exits import (
    apply_discovery_leg_exit_prices,
    build_discovery_exit_policy,
)


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(CYCLE)


@pytest.fixture
def store(tmp_path: Path, clock: FrozenClock) -> Iterator[TradingStore]:
    trading_store = TradingStore.open(tmp_path / "paper.sqlite", clock=clock)
    yield trading_store
    trading_store.close()


def _following_week_legs() -> tuple[PositionLegState, ...]:
    return (
        f.position_leg_state(
            leg_id="put",
            contract=f.option_contract(
                symbol="NIFTY26OCT22800PE",
                strike=Decimal("22800"),
                option_type=OptionType.PUT,
            ),
            side=Side.BUY,
            average_entry_price=f.price("137.25"),
        ),
        f.position_leg_state(
            leg_id="call",
            contract=f.option_contract(
                symbol="NIFTY26OCT22900CE",
                strike=Decimal("22900"),
                option_type=OptionType.CALL,
            ),
            side=Side.BUY,
            average_entry_price=f.price("202.75"),
        ),
    )


def _discovery_runner(store: TradingStore, clock: FrozenClock) -> PaperRunner:
    ids = SequentialIdFactory(clock.instant)
    broker = PaperBroker.from_fixtures(BROKER_FIXTURES, clock=clock, id_factory=ids)
    return PaperRunner(
        account_config=_paper_config(),  # type: ignore[arg-type]
        risk_policy=load_risk_policy(ROOT / "config" / "risk.yaml"),
        store=store,
        broker=broker,
        clock=clock,
        id_factory=ids,
        discovery_config=DISCOVERY,
    )


def _open_strangle(
    runner: PaperRunner,
    store: TradingStore,
    legs: tuple[PositionLegState, ...],
) -> None:
    legs = apply_discovery_leg_exit_prices(legs, EXITS)
    intent = _strangle_intent(legs)
    policy = build_discovery_exit_policy(
        intent,
        legs,
        f.exit_template(stop_distance_ticks=40),
        trade_id="TRD-A25",
        policy_id="EXIT-A25",
        initialized_at=NOW,
        config=EXITS,
        scope=ExitScope.STRATEGY_PNL,
    )
    position = f.position_state(
        trade_id="TRD-A25",
        state=TradeState.OPEN,
        legs=legs,
        exit_policy=policy,
        protective_order_ids=("PROT-1",),
        opened_at=NOW,
    )
    store.upsert_position_lifecycle(
        PositionLifecycleRecord(
            trade_id=position.trade_id,
            position=position,
            intent=intent,
            risk_decision=f.risk_decision(intent_id=intent.intent_id),
            holding_style=HoldingStyle.INTRADAY,
            as_of=position.as_of,
        ),
        event_id="PLC-A25",
    )
    runner.recover_lifecycle()


def _rest_quote(mid: str) -> MarketQuote:
    tick = TickSize.of("0.05")
    half = Decimal("0.025")
    value = Decimal(mid)
    bid = Price.snap(value - half, tick)
    ask = Price.snap(value + half, tick)
    return f.quote(bid=bid, ask=ask, last=Price.snap(value, tick))


class TestRestProtectionStructureCheck:
    def test_held_leg_outside_chain_is_protected_via_rest(
        self, store: TradingStore, clock: FrozenClock
    ) -> None:
        """Invariant 6: structure exits must see REST quotes for off-chain legs."""
        runner = _discovery_runner(store, clock)
        legs = _following_week_legs()
        _open_strangle(runner, store, legs)
        put_symbol = legs[0].contract.symbol
        call_symbol = legs[1].contract.symbol
        cycle_snapshots = {
            put_symbol: _mid_snapshot(legs[0].contract, mid="140.00"),
        }
        runner.on_quote_update(
            {call_symbol: _rest_quote("205.00")},
            source=QuoteMonitorSource.REST,
            received_at=clock.now_utc(),
            quote_max_age_ms=DISCOVERY.hard_quote_max_age_ms,
        )
        runner.manage_exits(cycle_snapshots)
        position = runner.trade_manager.get_position("TRD-A25")
        assert position is not None
        assert not position.software_stop_unavailable
        assert not position.protection_degraded
        assert position.unprotected_reason is None

    def test_missing_off_chain_leg_still_blocks_without_rest(
        self, store: TradingStore, clock: FrozenClock
    ) -> None:
        """Invariant 6: absent REST quotes keep structure protection blocked."""
        runner = _discovery_runner(store, clock)
        legs = _following_week_legs()
        _open_strangle(runner, store, legs)
        put_symbol = legs[0].contract.symbol
        runner.manage_exits({put_symbol: _mid_snapshot(legs[0].contract, mid="140.00")})
        position = runner.trade_manager.get_position("TRD-A25")
        assert position is not None
        assert position.software_stop_unavailable
        assert "structure leg quote missing" in (position.unprotected_reason or "")


class TestMonitorSymbols:
    def test_monitor_symbols_use_open_position_legs_only(
        self, store: TradingStore, clock: FrozenClock
    ) -> None:
        runner = _discovery_runner(store, clock)
        legs = apply_discovery_leg_exit_prices(_strangle_legs(), EXITS)
        intent = _strangle_intent(legs)
        policy = build_discovery_exit_policy(
            intent,
            legs,
            f.exit_template(stop_distance_ticks=40),
            trade_id="TRD-MON",
            policy_id="EXIT-MON",
            initialized_at=NOW,
            config=EXITS,
            scope=ExitScope.STRATEGY_PNL,
        )
        open_leg = legs[0]
        position = f.position_state(
            trade_id="TRD-MON",
            state=TradeState.OPEN,
            legs=(open_leg,),
            exit_policy=policy,
            protective_order_ids=("PROT-1",),
            opened_at=NOW,
        )
        store.upsert_position_lifecycle(
            PositionLifecycleRecord(
                trade_id=position.trade_id,
                position=position,
                intent=intent,
                risk_decision=f.risk_decision(intent_id=intent.intent_id),
                holding_style=HoldingStyle.INTRADAY,
                as_of=position.as_of,
            ),
            event_id="PLC-MON",
        )
        runner.recover_lifecycle()
        assert runner.monitor_symbols() == (open_leg.contract.symbol,)

    def test_monitor_symbols_drop_closed_trades(
        self, store: TradingStore, clock: FrozenClock
    ) -> None:
        runner = _discovery_runner(store, clock)
        legs = apply_discovery_leg_exit_prices(_strangle_legs(), EXITS)
        _open_strangle(runner, store, legs)
        assert len(runner.monitor_symbols()) == 2
        closed = runner.trade_manager.get_position("TRD-A25")
        assert closed is not None
        runner.trade_manager.restore_position(
            closed.model_copy(update={"state": TradeState.CLOSED})
        )
        runner._open_book.pop("TRD-A25", None)
        assert runner.monitor_symbols() == ()
