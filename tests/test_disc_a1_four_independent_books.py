"""Tests for DISC-A1: Four independent books with daily compounding."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import tests.factories as f
from trading.config.discovery import load_discovery_config
from trading.domain.clock import FrozenClock
from trading.domain.contracts.lifecycle import PositionLifecycleRecord
from trading.domain.contracts.order import OrderEvent
from trading.domain.enums import (
    ExecutionMode,
    HoldingStyle,
    ModeId,
    OrderState,
    Side,
    TradeState,
)
from trading.domain.primitives import Currency, Money
from trading.risk.limits import build_sizing_limits
from trading.risk.mode_ledger import FourModeBook
from trading.storage.trading_store import TradingEventType, TradingStore

ROOT = Path(__file__).resolve().parent.parent
DISCOVERY_CFG = load_discovery_config(ROOT / "config" / "discovery.yaml")


def _record_closed_trade(
    store: TradingStore,
    *,
    trade_id: str,
    mode_id: ModeId,
    close_time: datetime,
    gross_pnl: Decimal,
) -> None:
    contract = f.option_contract()
    entry_leg = f.position_leg_state(
        leg_id=f"leg-{trade_id}",
        contract=contract,
        side=Side.BUY,
        quantity_contracts=1,
        average_entry_price=f.price("100000.00"),
    )
    closed_pos = f.position_state(
        trade_id=trade_id,
        intent_id=f"INT-{trade_id}",
        strategy_id="strat-1",
        execution_mode=ExecutionMode.PAPER,
        state=TradeState.CLOSED,
        legs=(entry_leg,),
        entry_legs=(entry_leg,),
        exit_policy=f.exit_policy(trade_id=trade_id),
        opened_at=close_time,
        as_of=close_time,
    )
    closed_lifecycle = PositionLifecycleRecord(
        trade_id=trade_id,
        position=closed_pos,
        intent=f.intent(intent_id=f"INT-{trade_id}", mode_id=mode_id),
        risk_decision=f.risk_decision(intent_id=f"INT-{trade_id}"),
        holding_style=HoldingStyle.INTRADAY,
        exit_order_ids=(f"ORD-X-{trade_id}",),
        as_of=close_time,
    )
    store.upsert_position_lifecycle(closed_lifecycle, event_id=f"EVT-LC-{trade_id}")

    entry_order = OrderEvent(
        event_id=f"EVT-ENTRY-{trade_id}",
        identity=f.order_identity(
            internal_order_id=f"ORD-E-{trade_id}",
            intent_id=f"INT-{trade_id}",
            trade_id=trade_id,
            idempotency_key=f"IDEM-E-{trade_id}",
        ),
        command=f.order_command(contract=contract, side=Side.BUY, quantity_contracts=1),
        state=OrderState.FILLED,
        attempt_number=1,
        filled_quantity=1,
        average_fill_price=f.price("100000.00"),
        sent_at=close_time,
        received_at=close_time,
    )
    exit_price = str(Decimal("100000.00") + gross_pnl)
    exit_order = OrderEvent(
        event_id=f"EVT-EXIT-{trade_id}",
        identity=f.order_identity(
            internal_order_id=f"ORD-X-{trade_id}",
            intent_id=f"INT-{trade_id}",
            trade_id=trade_id,
            idempotency_key=f"IDEM-X-{trade_id}",
        ),
        command=f.order_command(
            contract=contract, side=Side.SELL, quantity_contracts=1
        ),
        state=OrderState.FILLED,
        attempt_number=1,
        filled_quantity=1,
        average_fill_price=f.price(exit_price),
        sent_at=close_time,
        received_at=close_time,
    )
    store.append(
        TradingEventType.ORDER_EVENT, entry_order, event_id=f"EVT-E-{trade_id}"
    )
    store.append(TradingEventType.ORDER_EVENT, exit_order, event_id=f"EVT-X-{trade_id}")


class TestDiscAFourIndependentBooks:
    def test_fresh_store_discovery_all_modes_seven_lakh(self, tmp_path: Path) -> None:
        db_path = tmp_path / "trading.sqlite"
        clock = FrozenClock(datetime(2026, 9, 28, 9, 15, tzinfo=UTC))
        store = TradingStore.open(db_path, clock=clock)
        book = FourModeBook.reconstruct_from_store(
            store,
            date(2026, 9, 28),
            discovery_config=DISCOVERY_CFG,
        )
        for mode in (
            ModeId.M1_CAS,
            ModeId.M2_DIRECTIONAL,
            ModeId.M3_TACTICAL_POSITIONAL,
            ModeId.M4_STRATEGIC_POSITIONAL,
        ):
            ledger = book.get_ledger(mode)
            assert ledger.allocated_capital == Money.of("700000", Currency.INR)
            assert ledger.reference_capital == Money.of("700000", Currency.INR)
        assert book.total_equity == Money.of("2800000", Currency.INR)

    def test_prior_day_realized_net_compounds_independently(
        self, tmp_path: Path
    ) -> None:
        db_path = tmp_path / "trading.sqlite"
        clock = FrozenClock(datetime(2026, 9, 28, 9, 15, tzinfo=UTC))
        store = TradingStore.open(db_path, clock=clock)
        yesterday = datetime(2026, 9, 25, 14, 0, tzinfo=UTC)
        today = date(2026, 9, 28)

        _record_closed_trade(
            store,
            trade_id="T-M2-1",
            mode_id=ModeId.M2_DIRECTIONAL,
            close_time=yesterday,
            gross_pnl=Decimal("12000"),
        )
        _record_closed_trade(
            store,
            trade_id="T-M4-1",
            mode_id=ModeId.M4_STRATEGIC_POSITIONAL,
            close_time=yesterday,
            gross_pnl=Decimal("-5000"),
        )

        book = FourModeBook.reconstruct_from_store(
            store,
            today,
            discovery_config=DISCOVERY_CFG,
        )

        # M2: starting 700k + 12k realized net = 712k (charges in test fixture are zero or conservative net)
        m2_ledger = book.get_ledger(ModeId.M2_DIRECTIONAL)
        m4_ledger = book.get_ledger(ModeId.M4_STRATEGIC_POSITIONAL)
        m1_ledger = book.get_ledger(ModeId.M1_CAS)
        m3_ledger = book.get_ledger(ModeId.M3_TACTICAL_POSITIONAL)

        # Prior net realized increases M2 and reduces M4
        assert m2_ledger.allocated_capital > Money.of("700000", Currency.INR)
        assert m4_ledger.allocated_capital < Money.of("700000", Currency.INR)
        assert m1_ledger.allocated_capital == Money.of("700000", Currency.INR)
        assert m3_ledger.allocated_capital == Money.of("700000", Currency.INR)

    def test_intraday_pnl_does_not_change_todays_allocated_capital(
        self, tmp_path: Path
    ) -> None:
        db_path = tmp_path / "trading.sqlite"
        clock = FrozenClock(datetime(2026, 9, 28, 9, 15, tzinfo=UTC))
        store = TradingStore.open(db_path, clock=clock)
        today_date = date(2026, 9, 28)
        today_time = datetime(2026, 9, 28, 10, 30, tzinfo=UTC)

        _record_closed_trade(
            store,
            trade_id="T-M2-TODAY",
            mode_id=ModeId.M2_DIRECTIONAL,
            close_time=today_time,
            gross_pnl=Decimal("5000"),
        )

        book = FourModeBook.reconstruct_from_store(
            store,
            today_date,
            discovery_config=DISCOVERY_CFG,
        )

        ledger_m2 = book.get_ledger(ModeId.M2_DIRECTIONAL)
        # Allocated capital stays fixed for the whole session
        assert ledger_m2.allocated_capital == Money.of("700000", Currency.INR)
        assert ledger_m2.realized_gross_pnl_today == Money.of("5000", Currency.INR)

    def test_no_cross_mode_contamination_on_m4_loss(self, tmp_path: Path) -> None:
        db_path = tmp_path / "trading.sqlite"
        clock = FrozenClock(datetime(2026, 9, 28, 9, 15, tzinfo=UTC))
        store = TradingStore.open(db_path, clock=clock)
        yesterday = datetime(2026, 9, 25, 14, 0, tzinfo=UTC)
        today = date(2026, 9, 28)

        _record_closed_trade(
            store,
            trade_id="T-M4-LOSS",
            mode_id=ModeId.M4_STRATEGIC_POSITIONAL,
            close_time=yesterday,
            gross_pnl=Decimal("-50000"),
        )

        book = FourModeBook.reconstruct_from_store(
            store,
            today,
            discovery_config=DISCOVERY_CFG,
        )

        ledger_m2 = book.get_ledger(ModeId.M2_DIRECTIONAL)
        assert ledger_m2.allocated_capital == Money.of("700000", Currency.INR)
        assert ledger_m2.available_capital == Money.of("700000", Currency.INR)

    def test_mid_day_restart_reproduces_same_equities(self, tmp_path: Path) -> None:
        db_path = tmp_path / "trading.sqlite"
        clock = FrozenClock(datetime(2026, 9, 28, 9, 15, tzinfo=UTC))
        store = TradingStore.open(db_path, clock=clock)
        yesterday = datetime(2026, 9, 25, 14, 0, tzinfo=UTC)
        today_date = date(2026, 9, 28)
        today_time = datetime(2026, 9, 28, 11, 0, tzinfo=UTC)

        _record_closed_trade(
            store,
            trade_id="T-Y",
            mode_id=ModeId.M2_DIRECTIONAL,
            close_time=yesterday,
            gross_pnl=Decimal("10000"),
        )
        _record_closed_trade(
            store,
            trade_id="T-T",
            mode_id=ModeId.M2_DIRECTIONAL,
            close_time=today_time,
            gross_pnl=Decimal("3000"),
        )

        book1 = FourModeBook.reconstruct_from_store(
            store, today_date, discovery_config=DISCOVERY_CFG
        )
        book2 = FourModeBook.reconstruct_from_store(
            store, today_date, discovery_config=DISCOVERY_CFG
        )

        for mode in (
            ModeId.M1_CAS,
            ModeId.M2_DIRECTIONAL,
            ModeId.M3_TACTICAL_POSITIONAL,
            ModeId.M4_STRATEGIC_POSITIONAL,
        ):
            assert (
                book1.get_ledger(mode).allocated_capital
                == book2.get_ledger(mode).allocated_capital
            )
            assert (
                book1.get_ledger(mode).realized_pnl_today
                == book2.get_ledger(mode).realized_pnl_today
            )

    def test_strict_profile_remains_unchanged(self, tmp_path: Path) -> None:
        db_path = tmp_path / "trading.sqlite"
        clock = FrozenClock(datetime(2026, 9, 28, 9, 15, tzinfo=UTC))
        store = TradingStore.open(db_path, clock=clock)
        book = FourModeBook.reconstruct_from_store(
            store,
            date(2026, 9, 28),
        )
        assert book.get_ledger(ModeId.M1_CAS).allocated_capital == Money.of(
            "70000", Currency.INR
        )
        assert book.get_ledger(ModeId.M2_DIRECTIONAL).allocated_capital == Money.of(
            "196000", Currency.INR
        )
        assert book.get_ledger(
            ModeId.M3_TACTICAL_POSITIONAL
        ).allocated_capital == Money.of("210000", Currency.INR)
        assert book.get_ledger(
            ModeId.M4_STRATEGIC_POSITIONAL
        ).allocated_capital == Money.of("224000", Currency.INR)
        assert book.total_equity == Money.of("700000", Currency.INR)

    def test_sizing_limits_uses_mode_independent_equity(self, tmp_path: Path) -> None:
        from trading.config.loader import load_config
        from trading.config.risk_policy import load_risk_policy

        db_path = tmp_path / "trading.sqlite"
        clock = FrozenClock(datetime(2026, 9, 28, 9, 15, tzinfo=UTC))
        store = TradingStore.open(db_path, clock=clock)
        yesterday = datetime(2026, 9, 25, 14, 0, tzinfo=UTC)
        today = date(2026, 9, 28)

        _record_closed_trade(
            store,
            trade_id="T-M2-S",
            mode_id=ModeId.M2_DIRECTIONAL,
            close_time=yesterday,
            gross_pnl=Decimal("12000"),
        )
        book = FourModeBook.reconstruct_from_store(
            store, today, discovery_config=DISCOVERY_CFG
        )
        m2_ledger = book.get_ledger(ModeId.M2_DIRECTIONAL)
        # Starting 700,000 + 12,000 gross - 100 estimated charges = 711,900
        assert m2_ledger.allocated_capital == Money.of("711900.00", Currency.INR)

        acct = load_config(ROOT / "config" / "base.yaml")
        risk_policy = load_risk_policy(ROOT / "config" / "risk.yaml")

        portfolio = f.portfolio_snapshot(
            exposure=f.exposure(
                equity=book.total_equity,
                margin_used=Money.zero(Currency.INR),
                margin_available=book.total_equity,
            ),
            reserved_capital=Money.zero(Currency.INR),
        )
        limits_m2 = build_sizing_limits(
            portfolio,
            acct.config.risk,
            risk_policy.config,
            "directional_spread",
            config_version="1",
            mode_id=ModeId.M2_DIRECTIONAL,
            mode_ledger=m2_ledger,
        )
        # M2 policy per_trade_loss_cap_fraction is 0.04
        # reference_capital 711,900 * 0.04 = 28,476
        assert limits_m2.max_loss_per_trade == Money.of("28476.00", Currency.INR)
        assert limits_m2.strategy_allocation_remaining == Money.of(
            "711900.00", Currency.INR
        )
