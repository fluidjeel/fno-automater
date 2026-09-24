"""Tests for Phase P2: Mode Ledgers, Limits, and Reservations.

Verifies:
- ModeLedger math: equity, conservative equity, reference capital, available capital, caps, budgets.
- FourModeBook: ₹7L book partitioned 10/20/30/40, no cross-mode borrow, reservation holds.
- Store reconstruction: crash recovery restores active reservations, open margins, and realized day loss.
- CapitalReservation: carries mode_id and idempotency_key.
- Sizing limits & RiskGateway: mode-based bounds, MIN_LOT_EXCEEDS_BUDGET on over-budget cost, reservation tagging.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from tests import factories as f
from tests.test_risk_gateway import instrument_spec, option_snapshot
from trading.broker.paper.adapter import PaperBroker
from trading.config.loader import load_config
from trading.config.risk_policy import load_risk_policy_text
from trading.domain.clock import FrozenClock
from trading.domain.contracts.lifecycle import PositionLifecycleRecord
from trading.domain.contracts.mode_policy import load_modes_config
from trading.domain.contracts.order import (
    OrderCommand,
    OrderEvent,
    OrderIdentity,
)
from trading.domain.contracts.reservation import CapitalReservation
from trading.domain.enums import (
    ExecutionMode,
    FamilyId,
    HoldingStyle,
    ModeId,
    OrderState,
    OrderType,
    ReasonCode,
    ReservationState,
    RiskAction,
    Side,
    TimeInForce,
    TradeState,
)
from trading.domain.ids import SequentialIdFactory
from trading.domain.primitives import Currency, Money, Price
from trading.risk.gateway import RiskGateway, RiskGatewayRequest
from trading.risk.limits import build_sizing_limits
from trading.risk.mode_ledger import FourModeBook, ModeLedger
from trading.risk.reservation import CapitalReservationService
from trading.storage.trading_store import TradingEventType, TradingStore

ROOT = Path(__file__).resolve().parent.parent
BROKER_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "broker"
NOW = datetime(2026, 9, 14, 4, 0, tzinfo=UTC)

RAW_RISK_POLICY = """
schema_version: "1"
policy_version: "6"

strategy_allocations:
  cas_microstructure:
    allocation_fraction: "0.10"
  positional_long_option:
    allocation_fraction: "0.20"
  debit_spread:
    allocation_fraction: "0.30"
  defined_risk_multileg:
    allocation_fraction: "0.40"

underlying_concentration_fraction: "0.25"
options_premium_budget_fraction: "0.15"
slippage_buffer_fraction: "0.0025"
charges_per_lot:
  amount: "50"
  currency: INR

net_delta_limit: 500
net_vega_limit: "5000"
expiry_day_notional_fraction: "0.35"
directional_agreement_max: "0.85"
single_event_exposure_fraction: "0.25"
tail_budget_fraction: "0.08"
decision_ttl_seconds: 300
"""

ACCOUNT_CONFIG = load_config(ROOT / "config" / "base.yaml")
RISK_POLICY = load_risk_policy_text(RAW_RISK_POLICY)


def _money(val: str) -> Money:
    return Money.of(val, Currency.INR)


def _price(val: str) -> Price:
    return f.price(val)


# ---------------------------------------------------------------------------
# 1. ModeLedger Unit Tests
# ---------------------------------------------------------------------------


def test_mode_ledger_initialization_and_properties() -> None:
    """Verifies ModeLedger field defaults, equity, reference capital, available capital."""
    ledger = ModeLedger(
        mode_id=ModeId.M1_CAS,
        allocated_capital=_money("70000"),
    )
    assert ledger.mode_id is ModeId.M1_CAS
    assert ledger.allocated_capital == _money("70000")
    assert ledger.realized_pnl_today == _money("0")
    assert ledger.unrealized_pnl == _money("0")
    assert ledger.reserved_capital == _money("0")
    assert ledger.margin_used == _money("0")
    assert ledger.high_water_mark == _money("70000")
    assert ledger.drawdown == _money("0")

    # Invariants
    assert ledger.equity == _money("70000")
    assert ledger.conservative_equity == _money("70000")
    assert ledger.reference_capital == _money("70000")
    assert ledger.available_capital == _money("70000")
    assert ledger.free_cash == _money("70000")


def test_mode_ledger_conservative_equity_and_reference_capital() -> None:
    """Verifies conservative equity ignores unrealized gains and penalizes unrealized losses."""
    base = ModeLedger(
        mode_id=ModeId.M2_DIRECTIONAL,
        allocated_capital=_money("140000"),
    )

    # 1. Unrealized profit of ₹10,000 does NOT increase reference capital (Invariant)
    up_gain = base.model_copy(update={"unrealized_pnl": _money("10000")})
    assert up_gain.equity == _money("150000")
    assert up_gain.conservative_equity == _money("140000")
    assert up_gain.reference_capital == _money("140000")

    # 2. Unrealized loss of ₹15,000 penalizes conservative equity and reference capital
    up_loss = base.model_copy(update={"unrealized_pnl": _money("-15000")})
    assert up_loss.equity == _money("125000")
    assert up_loss.conservative_equity == _money("125000")
    assert up_loss.reference_capital == _money("125000")

    # 3. Realized loss of ₹20,000 penalizes reference capital
    realized_loss = base.model_copy(update={"realized_pnl_today": _money("-20000")})
    assert realized_loss.equity == _money("120000")
    assert realized_loss.conservative_equity == _money("120000")
    assert realized_loss.reference_capital == _money("120000")


def test_mode_ledger_loss_caps_and_budgets() -> None:
    """Verifies per-trade loss cap, daily budget, daily loss remaining, and daily loss breach."""
    ledger = ModeLedger(
        mode_id=ModeId.M1_CAS,
        allocated_capital=_money("70000"),
    )
    # M1 per-trade cap is 5% -> ₹3,500
    per_trade_cap = ledger.per_trade_loss_cap(Decimal("0.05"))
    assert per_trade_cap == _money("3500")

    # M1 daily budget fraction is 15% -> ₹10,500
    daily_budget = ledger.daily_loss_budget(Decimal("0.15"))
    assert daily_budget == _money("10500")

    # With zero realized loss today, daily_loss_remaining is full budget
    assert ledger.daily_loss_remaining(Decimal("0.15")) == _money("10500")
    assert ledger.daily_loss_breached(Decimal("0.15")) is False

    # Realized loss of ₹4,000 drops conservative equity to ₹66,000,
    # so reference capital becomes ₹66,000, daily budget is 15% * 66,000 = ₹9,900,
    # and remaining budget is ₹9,900 - ₹4,000 = ₹5,900.
    with_loss = ledger.model_copy(update={"realized_pnl_today": _money("-4000")})
    assert with_loss.daily_loss_remaining(Decimal("0.15")) == _money("5900")
    assert with_loss.daily_loss_breached(Decimal("0.15")) is False

    # Realized loss reaching budget (₹10,500) breaches daily limit
    breached = ledger.model_copy(update={"realized_pnl_today": _money("-10500")})
    assert breached.daily_loss_remaining(Decimal("0.15")) == _money("0")
    assert breached.daily_loss_breached(Decimal("0.15")) is True


def test_mode_ledger_reservations_and_fills() -> None:
    """Verifies can_reserve, record_reservation, release_reservation, and record_fill."""
    ledger = ModeLedger(
        mode_id=ModeId.M3_TACTICAL_POSITIONAL,
        allocated_capital=_money("210000"),
    )
    assert ledger.available_capital == _money("210000")
    assert ledger.can_reserve(_money("50000")) is True
    assert ledger.can_reserve(_money("250000")) is False

    # Record reservation
    reserved = ledger.record_reservation(_money("50000"))
    assert reserved.reserved_capital == _money("50000")
    assert reserved.available_capital == _money("160000")

    # Release reservation
    released = reserved.release_reservation(_money("20000"))
    assert released.reserved_capital == _money("30000")
    assert released.available_capital == _money("180000")

    # Record fill: converts reservation hold to margin_used
    filled = released.record_fill(margin=_money("30000"), premium=_money("0"))
    assert filled.reserved_capital == _money("0")
    assert filled.margin_used == _money("30000")
    assert filled.available_capital == _money("180000")


# ---------------------------------------------------------------------------
# 2. FourModeBook Unit Tests & No Cross-Mode Borrow
# ---------------------------------------------------------------------------


def test_four_mode_book_partitioning() -> None:
    """Verifies 10/20/30/40 partition of the ₹7,00,000 book."""
    book = FourModeBook()
    assert book.total_equity == _money("700000")

    m1 = book.get_ledger(ModeId.M1_CAS)
    m2 = book.get_ledger(ModeId.M2_DIRECTIONAL)
    m3 = book.get_ledger(ModeId.M3_TACTICAL_POSITIONAL)
    m4 = book.get_ledger(ModeId.M4_STRATEGIC_POSITIONAL)

    assert m1.allocated_capital == _money("70000")
    assert m2.allocated_capital == _money("140000")
    assert m3.allocated_capital == _money("210000")
    assert m4.allocated_capital == _money("280000")

    assert (
        m1.allocated_capital
        + m2.allocated_capital
        + m3.allocated_capital
        + m4.allocated_capital
    ) == _money("700000")


def test_four_mode_book_no_cross_mode_borrow() -> None:
    """Verifies strict isolation: Mode A cannot borrow from Mode B cash."""
    book = FourModeBook()

    # M1 has ₹70,000 available
    assert book.available_for(ModeId.M1_CAS) == _money("70000")
    assert book.available_for(ModeId.M2_DIRECTIONAL) == _money("140000")

    # Reserve ₹60,000 in M1 -> succeeds
    assert book.try_reserve(ModeId.M1_CAS, _money("60000")) is True
    assert book.available_for(ModeId.M1_CAS) == _money("10000")

    # M1 attempts to reserve ₹20,000 (exceeds M1's remaining ₹10,000) -> MUST FAIL CLOSED
    assert book.try_reserve(ModeId.M1_CAS, _money("20000")) is False
    assert book.available_for(ModeId.M1_CAS) == _money("10000")

    # M2's capital is completely untouched
    assert book.available_for(ModeId.M2_DIRECTIONAL) == _money("140000")
    assert book.try_reserve(ModeId.M2_DIRECTIONAL, _money("140000")) is True
    assert book.available_for(ModeId.M2_DIRECTIONAL) == _money("0")
    assert book.try_reserve(ModeId.M2_DIRECTIONAL, _money("1")) is False


# ---------------------------------------------------------------------------
# 3. CapitalReservation Contract Tests
# ---------------------------------------------------------------------------


def test_capital_reservation_mode_id_and_idempotency_key() -> None:
    """Verifies CapitalReservation holds mode_id and idempotency_key."""
    res = CapitalReservation(
        reservation_id="RES-1",
        intent_id="INT-1",
        strategy_id="cas_microstructure",
        mode_id=ModeId.M1_CAS,
        idempotency_key="INT-1",
        state=ReservationState.RESERVED,
        amount=_money("3500"),
        created_at=NOW,
        updated_at=NOW,
    )
    assert res.mode_id is ModeId.M1_CAS
    assert res.idempotency_key == "INT-1"

    # Round-trip serialization preserves fields
    cycled = res.round_trip()
    assert cycled.mode_id is ModeId.M1_CAS
    assert cycled.idempotency_key == "INT-1"
    assert cycled.amount == _money("3500")


# ---------------------------------------------------------------------------
# 4. Store Reconstruction Tests
# ---------------------------------------------------------------------------


def test_four_mode_book_reconstruct_from_store(tmp_path: Path) -> None:
    """Verifies crash recovery restores active reservations, open margins, and realized day loss."""
    clock = FrozenClock(NOW)
    store = TradingStore.open(tmp_path / "recon.db", clock=clock)

    # 1. Add active reservation for M1 (₹2,500)
    res_m1 = CapitalReservation(
        reservation_id="RES-M1",
        intent_id="INT-M1",
        strategy_id="cas_microstructure",
        mode_id=ModeId.M1_CAS,
        idempotency_key="INT-M1",
        state=ReservationState.RESERVED,
        amount=_money("2500"),
        created_at=NOW,
        updated_at=NOW,
    )
    store.upsert_reservation(res_m1)

    # 2. Add released reservation for M2 (should NOT hold capital)
    res_m2_released = CapitalReservation(
        reservation_id="RES-M2",
        intent_id="INT-M2",
        strategy_id="positional_long_option",
        mode_id=ModeId.M2_DIRECTIONAL,
        idempotency_key="INT-M2",
        state=ReservationState.RELEASED,
        amount=_money("4000"),
        created_at=NOW,
        updated_at=NOW,
        released_at=NOW,
    )
    store.upsert_reservation(res_m2_released)

    # 3. Add open position for M3 with margin requirement ₹35,000
    open_intent = f.intent(
        intent_id="INT-M3-OPEN",
        mode_id=ModeId.M3_TACTICAL_POSITIONAL,
        family_id=FamilyId.bull_call_debit,
    )
    open_decision = f.risk_decision(
        intent_id="INT-M3-OPEN",
        margin_required=_money("35000"),
    )
    open_pos = f.position_state(
        trade_id="TRD-M3",
        intent_id="INT-M3-OPEN",
        state=TradeState.OPEN,
        exit_policy=f.exit_policy(trade_id="TRD-M3"),
        opened_at=NOW,
    )
    open_lifecycle = PositionLifecycleRecord(
        trade_id="TRD-M3",
        position=open_pos,
        intent=open_intent,
        risk_decision=open_decision,
        holding_style=HoldingStyle.POSITIONAL,
        as_of=NOW,
    )
    store.upsert_position_lifecycle(open_lifecycle, event_id="EVT-M3-OPEN")

    # 4. Add closed trade for M2 with a realized loss today of ₹1,500
    # Entry: BUY 75 contracts @ 100.00
    # Exit: SELL 75 contracts @ 80.00 -> Loss = -20 * 75 = -1,500
    closed_intent = f.intent(
        intent_id="INT-M2-CLOSED",
        mode_id=ModeId.M2_DIRECTIONAL,
        family_id=FamilyId.long_call,
    )
    closed_decision = f.risk_decision(intent_id="INT-M2-CLOSED")
    contract = f.option_contract()
    entry_leg = f.position_leg_state(
        leg_id="leg-1",
        contract=contract,
        side=Side.BUY,
        quantity_contracts=75,
        average_entry_price=_price("100.00"),
    )
    closed_pos = f.position_state(
        trade_id="TRD-M2-CLOSED",
        intent_id="INT-M2-CLOSED",
        strategy_id="positional_long_option",
        strategy_version="1",
        experiment_id="EXP-1",
        execution_mode=ExecutionMode.PAPER,
        state=TradeState.CLOSED,
        legs=(entry_leg,),
        exit_policy=f.exit_policy(trade_id="TRD-M2-CLOSED"),
        opened_at=NOW,
        as_of=NOW,
    )
    closed_lifecycle = PositionLifecycleRecord(
        trade_id="TRD-M2-CLOSED",
        position=closed_pos,
        intent=closed_intent,
        risk_decision=closed_decision,
        holding_style=HoldingStyle.INTRADAY,
        exit_order_ids=("ORD-EXIT-1",),
        as_of=NOW,
    )
    store.upsert_position_lifecycle(closed_lifecycle, event_id="EVT-M2-CLOSED")

    # Record the exit fill order event in store
    exit_order = OrderEvent(
        event_id="EVT-EXIT-ORD-1",
        identity=OrderIdentity(
            internal_order_id="ORD-EXIT-1",
            client_order_id="CL-EXIT-1",
            idempotency_key="IDEMP-EXIT-1",
            intent_id="INT-M2-CLOSED",
            risk_decision_id="DEC-EXIT-1",
            trade_id="TRD-M2-CLOSED",
            correlation_id="COR-1",
            experiment_id="EXP-1",
            execution_mode=ExecutionMode.PAPER,
        ),
        command=OrderCommand(
            contract=contract,
            side=Side.SELL,
            order_type=OrderType.MARKET,
            time_in_force=TimeInForce.DAY,
            quantity_contracts=75,
        ),
        state=OrderState.FILLED,
        attempt_number=1,
        acknowledged_quantity=75,
        filled_quantity=75,
        average_fill_price=_price("80.00"),
        sent_at=NOW,
        received_at=NOW,
    )
    store.append(
        TradingEventType.ORDER_EVENT,
        exit_order,
        event_id="EVT-EXIT-FILL",
    )

    # Reconstruct FourModeBook from the store as of today
    session_date = NOW.date()
    reconstructed = FourModeBook.reconstruct_from_store(store, session_date)

    # Verify M1: ₹70,000 allocation, ₹2,500 reserved -> available = ₹67,500
    m1 = reconstructed.get_ledger(ModeId.M1_CAS)
    assert m1.allocated_capital == _money("70000")
    assert m1.reserved_capital == _money("2500")
    assert m1.available_capital == _money("67500")

    # Verify M2: ₹140,000 allocation, ₹0 active reservations, realized loss ₹1,500
    m2 = reconstructed.get_ledger(ModeId.M2_DIRECTIONAL)
    assert m2.allocated_capital == _money("140000")
    assert m2.reserved_capital == _money("0")
    assert m2.realized_pnl_today == _money("-1500")
    assert m2.equity == _money("138500")
    assert m2.available_capital == _money("138500")

    # Verify M3: ₹210,000 allocation, margin used ₹35,000 -> available = ₹175,000
    m3 = reconstructed.get_ledger(ModeId.M3_TACTICAL_POSITIONAL)
    assert m3.allocated_capital == _money("210000")
    assert m3.margin_used == _money("35000")
    assert m3.available_capital == _money("175000")

    # Verify M4: untouched clean ledger
    m4 = reconstructed.get_ledger(ModeId.M4_STRATEGIC_POSITIONAL)
    assert m4.allocated_capital == _money("280000")
    assert m4.available_capital == _money("280000")


# ---------------------------------------------------------------------------
# 5. build_sizing_limits with Mode Ledger Support
# ---------------------------------------------------------------------------


def test_build_sizing_limits_with_mode_ledger() -> None:
    """Verifies build_sizing_limits computes limits from mode reference and available capital."""
    portfolio = f.portfolio_snapshot(
        exposure=f.exposure(
            equity=_money("700000"),
            margin_used=_money("0"),
            margin_available=_money("700000"),
        ),
        reserved_capital=_money("0"),
    )
    modes_cfg = load_modes_config()

    # M1: ₹70,000 reference capital, 5% per-trade cap = ₹3,500, daily budget 15% = ₹10,500
    m1_ledger = ModeLedger(
        mode_id=ModeId.M1_CAS,
        allocated_capital=_money("70000"),
        reserved_capital=_money("10000"),
    )
    limits_m1 = build_sizing_limits(
        portfolio,
        ACCOUNT_CONFIG.config.risk,
        RISK_POLICY.config,
        "cas_microstructure",
        config_version="1",
        mode_id=ModeId.M1_CAS,
        modes_config=modes_cfg,
        mode_ledger=m1_ledger,
    )
    assert limits_m1.max_loss_per_trade == _money("3500")
    assert limits_m1.daily_loss_remaining == _money("10500")
    assert limits_m1.strategy_allocation_remaining == _money("60000")
    assert limits_m1.margin_available == _money("60000")


def test_build_sizing_limits_backward_compatibility() -> None:
    """Verifies build_sizing_limits preserves backward compatibility when mode_id is None."""
    portfolio = f.portfolio_snapshot(
        exposure=f.exposure(
            equity=_money("700000"),
            margin_available=_money("680000"),
        ),
        reserved_capital=_money("20000"),
    )
    limits_compat = build_sizing_limits(
        portfolio,
        ACCOUNT_CONFIG.config.risk,
        RISK_POLICY.config,
        "positional_long_option",
        config_version="1",
    )
    # Uses account risk 1% of 700k = ₹7,000
    assert limits_compat.max_loss_per_trade == _money("7000")
    # Uses strategy allocation fraction 20% of 700k = 140,000 - 20,000 reserved = 120,000
    assert limits_compat.strategy_allocation_remaining == _money("120000")


# ---------------------------------------------------------------------------
# 6. RiskGateway Integration Tests
# ---------------------------------------------------------------------------


def test_gateway_rejects_with_min_lot_exceeds_budget_when_over_cap(
    tmp_path: Path,
) -> None:
    """Verifies that an intent with cost exceeding mode loss cap rejects with MIN_LOT_EXCEEDS_BUDGET."""
    clock = FrozenClock(NOW)
    id_factory = SequentialIdFactory(clock.instant)
    store = TradingStore.open(tmp_path / "gw.db", clock=clock)
    broker = PaperBroker.from_fixtures(
        BROKER_FIXTURES, clock=clock, id_factory=id_factory
    )
    book = FourModeBook()

    gw = RiskGateway(
        account_config=ACCOUNT_CONFIG,
        risk_policy=RISK_POLICY,
        reservation_service=CapitalReservationService(
            store,
            clock=clock,
            id_factory=id_factory,
        ),
        margin_preview=broker,
        clock=clock,
        id_factory=id_factory,
        mode_book=book,
    )

    # Option ask is 92.00 -> 1 lot of 75 contracts costs ~₹6,950
    # M1 per-trade cap is ₹3,500. This exceeds budget!
    snap = option_snapshot(
        market=f.quote(
            bid=_price("91.95"),
            ask=_price("92.00"),
            bid_size=300,
            ask_size=300,
        )
    )
    intent = f.intent(
        snapshot_id=snap.snapshot_id,
        mode_id=ModeId.M1_CAS,
        family_id=FamilyId.long_call,
    )
    req = RiskGatewayRequest(
        intent=intent,
        feature_snapshot=snap,
        portfolio_snapshot=f.portfolio_snapshot(),
        instrument=instrument_spec(),
        event_risk_state=f.event_risk_state(),
    )

    decision = gw.evaluate(req)
    assert decision.action is RiskAction.REJECT
    assert decision.reason_codes == (ReasonCode.MIN_LOT_EXCEEDS_BUDGET,)
    assert len(decision.approved_legs) == 0


def test_gateway_approves_and_tags_reservation_with_mode_and_idempotency_key(
    tmp_path: Path,
) -> None:
    """Verifies that affordable mode trades approve and store reservations with mode_id & idempotency_key."""
    clock = FrozenClock(NOW)
    id_factory = SequentialIdFactory(clock.instant)
    store = TradingStore.open(tmp_path / "gw_app.db", clock=clock)
    broker = PaperBroker.from_fixtures(
        BROKER_FIXTURES, clock=clock, id_factory=id_factory
    )
    book = FourModeBook()

    gw = RiskGateway(
        account_config=ACCOUNT_CONFIG,
        risk_policy=RISK_POLICY,
        reservation_service=CapitalReservationService(
            store,
            clock=clock,
            id_factory=id_factory,
        ),
        margin_preview=broker,
        clock=clock,
        id_factory=id_factory,
        mode_book=book,
    )

    # Option ask is 20.00 -> 1 lot costs ~₹1,550 <= M1 cap (₹3,500)
    snap = option_snapshot(
        market=f.quote(
            bid=_price("19.95"),
            ask=_price("20.00"),
            bid_size=300,
            ask_size=300,
        )
    )
    intent = f.intent(
        snapshot_id=snap.snapshot_id,
        mode_id=ModeId.M1_CAS,
        family_id=FamilyId.long_call,
    )
    req = RiskGatewayRequest(
        intent=intent,
        feature_snapshot=snap,
        portfolio_snapshot=f.portfolio_snapshot(),
        instrument=instrument_spec(),
        event_risk_state=f.event_risk_state(),
    )

    decision = gw.evaluate(req)
    assert decision.action in (RiskAction.APPROVE, RiskAction.RESIZE)
    assert decision.capital_reservation_id is not None

    # Check that reservation in durable store carries mode_id and idempotency_key
    res = store.get_reservation(decision.capital_reservation_id)
    assert res is not None
    assert res.mode_id is ModeId.M1_CAS
    assert res.idempotency_key == intent.intent_id
    assert res.state is ReservationState.RESERVED
    assert res.amount > _money("0")

    # Check that FourModeBook ledger in gateway recorded the reservation
    m1_ledger = gw.mode_book.get_ledger(ModeId.M1_CAS)
    assert m1_ledger.reserved_capital == res.amount
    assert m1_ledger.available_capital == _money("70000") - res.amount
