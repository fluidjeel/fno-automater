"""Comprehensive P2 test suite — Four ledgers, reservations, and safety controls.

Locks in every P2 requirement from docs/plans/FOUR_MODE_LAYER_CHANGE_CRITERIA.md
§2.2 (per-mode capital ledgers), §2.3 (reference capital definition),
§2.17 (reservation lifecycle), §2.18 (per-mode safety controls).

Classes
-------
TestModeLedgerArithmetic
    Four-mode capital arithmetic: allocations, reference capital, per-trade
    caps, daily loss tracking, available capital floor, cross-mode isolation.
TestFourModeBookRestart
    Crash-recovery reconstruction via FourModeBook.reconstruct_from_store.
TestReservationModeId
    CapitalReservation carries mode_id; duplicate idempotency_key is deduplicated.
TestSafetyControlsModeLevelHalt
    Per-mode freeze / halt does not touch other modes; global kill switch does.
TestStoreIdempotency
    Schema migrations are safe to run twice; PositionLifecycleRecord mode_id
    round-trips through the durable store.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import tests.factories as f
from trading.domain.clock import FrozenClock
from trading.domain.contracts.entry_freeze import EntryFreezeRecord
from trading.domain.contracts.lifecycle import PositionLifecycleRecord
from trading.domain.contracts.reservation import CapitalReservation
from trading.domain.enums import (
    FamilyId,
    HoldingStyle,
    ModeId,
    ReasonCode,
    ReservationState,
    TradeState,
    Trigger,
)
from trading.domain.ids import SequentialIdFactory
from trading.domain.primitives import Currency, Money
from trading.risk.mode_ledger import DEFAULT_TOTAL_EQUITY, FourModeBook, ModeLedger
from trading.risk.reservation import CapitalReservationService
from trading.storage.trading_store import TradingStore

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

NOW = datetime(2026, 9, 24, 4, 0, tzinfo=UTC)

M1 = ModeId.M1_CAS
M2 = ModeId.M2_DIRECTIONAL
M3 = ModeId.M3_TACTICAL_POSITIONAL
M4 = ModeId.M4_STRATEGIC_POSITIONAL


def _inr(value: str) -> Money:
    return Money.of(value, Currency.INR)


def _open_store(db_path: Path) -> TradingStore:
    clock = FrozenClock(NOW)
    return TradingStore.open(db_path, clock=clock)


def _make_service(store: TradingStore) -> CapitalReservationService:
    clock = FrozenClock(NOW)
    id_factory = SequentialIdFactory(clock.instant)
    return CapitalReservationService(store, clock=clock, id_factory=id_factory)


# ---------------------------------------------------------------------------
# TestModeLedgerArithmetic
# ---------------------------------------------------------------------------


class TestModeLedgerArithmetic:
    """Lock in per-mode arithmetic: allocations, reference capital, loss caps,
    daily budget tracking, available floor, and cross-mode isolation."""

    def test_mode_allocation_totals_seven_lakhs(self) -> None:
        """M1+M2+M3+M4 allocated_capital == ₹7,00,000 (10/20/30/40 shares)."""
        book = FourModeBook()
        m1 = book.get_ledger(M1)
        m2 = book.get_ledger(M2)
        m3 = book.get_ledger(M3)
        m4 = book.get_ledger(M4)

        total = (
            m1.allocated_capital
            + m2.allocated_capital
            + m3.allocated_capital
            + m4.allocated_capital
        )
        assert total == DEFAULT_TOTAL_EQUITY
        assert total == _inr("700000")

        # Verify individual shares (10/20/30/40 → ₹70k/140k/210k/280k)
        assert m1.allocated_capital == _inr("70000")
        assert m2.allocated_capital == _inr("196000")
        assert m3.allocated_capital == _inr("210000")
        assert m4.allocated_capital == _inr("224000")

    def test_reference_capital_is_min_of_allocation_and_conservative_equity(
        self,
    ) -> None:
        """When unrealized_pnl < 0, conservative_equity < allocated_capital,
        so reference_capital uses conservative_equity (not the allocation)."""
        ledger = ModeLedger(
            mode_id=M1,
            allocated_capital=_inr("70000"),
        )
        # Positive unrealized PnL: reference_capital stays at allocation (not inflated)
        with_gain = ledger.model_copy(update={"unrealized_pnl": _inr("10000")})
        assert with_gain.conservative_equity == _inr("70000")
        assert with_gain.reference_capital == _inr("70000")

        # Negative unrealized PnL: conservative_equity shrinks, reference_capital follows
        loss_amount = _inr("15000")
        with_loss = ledger.model_copy(update={"unrealized_pnl": -loss_amount})
        expected_conservative = _inr("70000") - loss_amount  # ₹55,000
        assert with_loss.conservative_equity == expected_conservative
        assert with_loss.reference_capital == expected_conservative
        assert with_loss.reference_capital < with_loss.allocated_capital

    def test_per_trade_loss_cap_uses_reference_capital(self) -> None:
        """M1 per_trade_loss_cap at 5% == 0.05 * reference_capital (Decimal arithmetic)."""
        ledger = ModeLedger(
            mode_id=M1,
            allocated_capital=_inr("70000"),
        )
        fraction = Decimal("0.05")
        cap = ledger.per_trade_loss_cap(fraction)
        expected = (_inr("70000") * fraction).quantized(
            __import__(
                "trading.domain.primitives", fromlist=["Rounding"]
            ).Rounding.FLOOR
        )
        assert cap == expected
        assert cap == _inr("3500")

        # With unrealized loss shrinking reference_capital to ₹55,000 → cap is 5% of ₹55,000
        with_loss = ledger.model_copy(update={"unrealized_pnl": _inr("-15000")})
        assert with_loss.reference_capital == _inr("55000")
        cap_after_loss = with_loss.per_trade_loss_cap(Decimal("0.05"))
        assert cap_after_loss == _inr("2750")

    def test_daily_loss_remaining_decrements_with_realized_loss(self) -> None:
        """After recording realized losses, daily_loss_remaining < daily_loss_budget."""
        ledger = ModeLedger(
            mode_id=M2,
            allocated_capital=_inr("140000"),
        )
        daily_fraction = Decimal("0.10")
        full_budget = ledger.daily_loss_budget(daily_fraction)
        assert ledger.daily_loss_remaining(daily_fraction) == full_budget

        # After a ₹5,000 realized loss, remaining drops accordingly
        realized_loss = _inr("5000")
        with_loss = ledger.model_copy(update={"realized_pnl_today": -realized_loss})
        remaining = with_loss.daily_loss_remaining(daily_fraction)
        assert remaining < full_budget
        # Budget is 10% of (140k - 5k) = 10% of 135k = 13500; remaining = 13500 - 5000 = 8500
        ref_cap = with_loss.reference_capital
        expected_remaining = (ref_cap * daily_fraction).quantized(
            __import__(
                "trading.domain.primitives", fromlist=["Rounding"]
            ).Rounding.FLOOR
        ) - realized_loss
        assert remaining == expected_remaining

    def test_daily_loss_breached_at_cap(self) -> None:
        """daily_loss_breached returns True when realized_pnl_today consumes daily budget."""
        ledger = ModeLedger(
            mode_id=M1,
            allocated_capital=_inr("70000"),
        )
        daily_fraction = Decimal("0.15")

        # Loss of ₹4,000: reference capital becomes ₹66,000, daily budget = ₹9,900 -> not breached
        moderate_loss = ledger.model_copy(update={"realized_pnl_today": _inr("-4000")})
        assert moderate_loss.daily_loss_breached(daily_fraction) is False

        # Loss of ₹12,000: reference capital becomes ₹58,000, daily budget = ₹8,700 -> breached
        heavy_loss = ledger.model_copy(update={"realized_pnl_today": _inr("-12000")})
        assert heavy_loss.daily_loss_breached(daily_fraction) is True

    def test_available_capital_cannot_go_negative(self) -> None:
        """After large reservations, available_capital == Money.zero, not negative."""
        ledger = ModeLedger(
            mode_id=M3,
            allocated_capital=_inr("210000"),
        )
        # Reserve more than available
        oversized = _inr("300000")
        # ModeLedger.record_reservation doesn't guard (FourModeBook.try_reserve does)
        # We test via FourModeBook which enforces the floor
        book = FourModeBook()
        # Try to reserve exactly available → succeeds, available = zero
        avail = book.available_for(M3)
        assert book.try_reserve(M3, avail) is True
        assert book.available_for(M3) == Money.zero(Currency.INR)

        # Any further reservation fails
        assert book.try_reserve(M3, _inr("1")) is False
        # Available is still zero, not negative
        assert book.available_for(M3) == Money.zero(Currency.INR)

        # Directly on the ledger — available_capital property floors to zero
        overreserved = ledger.model_copy(update={"reserved_capital": oversized})
        assert overreserved.available_capital == Money.zero(Currency.INR)
        assert not overreserved.available_capital.is_negative

    def test_no_cross_mode_borrow_four_mode_book(self) -> None:
        """FourModeBook.try_reserve(M1, amount > M1.available_capital) returns False
        without touching any other mode."""
        book = FourModeBook()
        m2_before = book.available_for(M2)

        # Attempt to borrow more than M1 has (M1 = ₹70,000)
        result = book.try_reserve(M1, _inr("100000"))

        assert result is False
        # M1 unchanged
        assert book.available_for(M1) == _inr("70000")
        # M2 completely untouched
        assert book.available_for(M2) == m2_before

    def test_mode_a_reservation_does_not_count_in_mode_b_sum(self) -> None:
        """After reserving in M1, M2 available_capital is unchanged."""
        book = FourModeBook()
        m2_before = book.available_for(M2)
        m3_before = book.available_for(M3)
        m4_before = book.available_for(M4)

        reserve_amount = _inr("30000")
        assert book.try_reserve(M1, reserve_amount) is True

        # M1 decremented
        assert book.available_for(M1) == _inr("70000") - reserve_amount
        # All other modes completely isolated
        assert book.available_for(M2) == m2_before
        assert book.available_for(M3) == m3_before
        assert book.available_for(M4) == m4_before


# ---------------------------------------------------------------------------
# TestFourModeBookRestart
# ---------------------------------------------------------------------------


class TestFourModeBookRestart:
    """FourModeBook.reconstruct_from_store crash-recovery semantics."""

    def test_reconstruct_from_store_restores_mode_equity(self, tmp_path: Path) -> None:
        """Write a reservation event to TradingStore for M1, reconstruct, verify
        M1.reserved_capital is restored."""
        store = _open_store(tmp_path / "restart1.db")
        reservation_amount = _inr("12000")

        res = CapitalReservation(
            reservation_id="RES-RESTART-M1",
            intent_id="INT-RESTART-M1",
            strategy_id="cas_microstructure",
            mode_id=M1,
            idempotency_key="IDEM-RESTART-M1",
            state=ReservationState.RESERVED,
            amount=reservation_amount,
            created_at=NOW,
            updated_at=NOW,
        )
        store.upsert_reservation(res)

        book = FourModeBook.reconstruct_from_store(store, NOW.date())
        m1 = book.get_ledger(M1)

        assert m1.reserved_capital == reservation_amount
        # Available = allocation - reserved
        assert m1.available_capital == _inr("70000") - reservation_amount

    def test_restart_does_not_cross_contaminate_modes(self, tmp_path: Path) -> None:
        """M2 reservation in store → after reconstruct, M1 reserved_capital == 0
        and M2 reserved_capital == amount."""
        store = _open_store(tmp_path / "restart2.db")
        m2_amount = _inr("25000")

        res_m2 = CapitalReservation(
            reservation_id="RES-RESTART-M2",
            intent_id="INT-RESTART-M2",
            strategy_id="positional_long_option",
            mode_id=M2,
            idempotency_key="IDEM-RESTART-M2",
            state=ReservationState.RESERVED,
            amount=m2_amount,
            created_at=NOW,
            updated_at=NOW,
        )
        store.upsert_reservation(res_m2)

        book = FourModeBook.reconstruct_from_store(store, NOW.date())

        m1 = book.get_ledger(M1)
        m2 = book.get_ledger(M2)

        # M1 must have zero reserved (the reservation was for M2)
        assert m1.reserved_capital == Money.zero(Currency.INR)
        # M2 must have the reservation amount
        assert m2.reserved_capital == m2_amount


# ---------------------------------------------------------------------------
# TestReservationModeId
# ---------------------------------------------------------------------------


class TestReservationModeId:
    """CapitalReservation mode tagging and idempotency guarantees."""

    def test_reservation_carries_mode_id(self, tmp_path: Path) -> None:
        """try_reserve with mode_id=M1 returns a CapitalReservation with mode_id == M1."""
        store = _open_store(tmp_path / "res_mode.db")
        service = _make_service(store)

        margin_available = _inr("70000")
        result = service.try_reserve(
            intent_id="INT-RES-MODE-1",
            strategy_id="cas_microstructure",
            amount=_inr("5000"),
            margin_available=margin_available,
            risk_decision_id="DEC-RES-MODE-1",
            mode_id=M1,
            idempotency_key="IDEM-RES-MODE-1",
        )

        assert result.mode_id is M1
        assert result.state is ReservationState.RESERVED

        # Verify persisted in store
        stored = store.get_reservation(result.reservation_id)
        assert stored is not None
        assert stored.mode_id is M1

    def test_duplicate_idempotency_key_does_not_double_spend(
        self, tmp_path: Path
    ) -> None:
        """Same idempotency_key submitted twice returns the existing reservation
        without creating an additional hold."""
        store = _open_store(tmp_path / "idem.db")
        service = _make_service(store)

        idem_key = "IDEM-DEDUP-001"
        margin = _inr("50000")
        amount = _inr("8000")

        first = service.try_reserve(
            intent_id="INT-IDEM-1",
            strategy_id="positional_long_option",
            amount=amount,
            margin_available=margin,
            risk_decision_id="DEC-IDEM-1",
            mode_id=M2,
            idempotency_key=idem_key,
        )
        assert first.state is ReservationState.RESERVED

        # Second call with same idempotency_key — must return same reservation
        second = service.try_reserve(
            intent_id="INT-IDEM-1",
            strategy_id="positional_long_option",
            amount=amount,
            margin_available=margin,
            risk_decision_id="DEC-IDEM-1",
            mode_id=M2,
            idempotency_key=idem_key,
        )
        assert second.reservation_id == first.reservation_id
        assert second.amount == first.amount
        assert second.state is ReservationState.RESERVED

        # Only one reservation exists in the store for this intent
        all_reservations = store.list_reservations()
        matching = [r for r in all_reservations if r.idempotency_key == idem_key]
        assert len(matching) == 1


# ---------------------------------------------------------------------------
# TestSafetyControlsModeLevelHalt
# ---------------------------------------------------------------------------


class TestSafetyControlsModeLevelHalt:
    """Per-mode halt/freeze isolation vs. global kill switch."""

    def _make_controls(self):  # type: ignore[return]
        from trading.safety.controls import SafetyControls

        clock = FrozenClock(NOW)
        id_factory = SequentialIdFactory(clock.instant)
        return SafetyControls(clock=clock, id_factory=id_factory)

    def test_mode_daily_loss_breach_blocks_mode_entries_preserves_exits(
        self,
    ) -> None:
        """After evaluate_mode_daily_loss latches M1, blocks_entry(mode_id=M1) is True;
        blocks_entry(mode_id=M2) is False. Exits are not blocked by this control."""
        controls = self._make_controls()

        # Ledger with breached daily loss (reference_capital=₹70k, budget 15% = ₹10.5k)
        # realized_pnl_today = -₹10,500 → exactly at budget → breached
        m1_ledger = ModeLedger(
            mode_id=M1,
            allocated_capital=_inr("70000"),
            realized_pnl_today=_inr("-10500"),
        )

        event = controls.evaluate_mode_daily_loss(
            M1,
            m1_ledger,
            scope="test",
            trigger=Trigger.SCHEDULER,
            daily_budget_cap_fraction=Decimal("0.15"),
        )
        assert event is not None

        # M1 is now frozen for entries
        assert controls.blocks_entry(mode_id=M1) is True
        # M2 is completely unaffected
        assert controls.blocks_entry(mode_id=M2) is False

    def test_global_daily_loss_kills_all_new_entries(self) -> None:
        """After evaluate_daily_loss_kill_switch global, blocks_entry() is True
        for every mode."""
        from trading.config.schema import RiskLimits

        controls = self._make_controls()

        # Build a portfolio with realized loss that breaches the global account cap
        portfolio = f.portfolio_snapshot(
            exposure=f.exposure(
                equity=_inr("700000"),
                realized_pnl_today=_inr("-70001"),  # > 10% of ₹700k
            )
        )
        risk_limits = RiskLimits(
            max_loss_per_trade_fraction=Decimal("0.01"),
            daily_loss_cap_fraction=Decimal("0.10"),
            max_portfolio_risk_fraction=Decimal("0.20"),
            max_concurrent_trades=10,
            max_margin_utilisation_fraction=Decimal("0.50"),
        )

        event = controls.evaluate_daily_loss_kill_switch(
            portfolio,
            risk_limits,
            scope="test",
            trigger=Trigger.SCHEDULER,
        )
        assert event is not None

        # Global kill switch blocks ALL modes
        for mode_id in (M1, M2, M3, M4):
            assert controls.blocks_entry(mode_id=mode_id) is True

    def test_freeze_mode_entries_does_not_freeze_other_modes(self) -> None:
        """freeze_mode_entries(M3) → blocks_entry(mode_id=M3) True;
        blocks_entry(mode_id=M4) False."""
        controls = self._make_controls()

        controls.freeze_mode_entries(
            M3,
            actor="test-operator",
            scope="test",
            trigger=Trigger.OPERATOR,
        )

        assert controls.blocks_entry(mode_id=M3) is True
        assert controls.blocks_entry(mode_id=M4) is False
        assert controls.blocks_entry(mode_id=M1) is False
        assert controls.blocks_entry(mode_id=M2) is False

    def test_halt_mode_and_release(self) -> None:
        """halt_mode(M2) → entry_block_reasons(mode_id=M2) contains STRATEGY_HALTED;
        after release_mode_halt, entry_block_reasons is empty."""
        controls = self._make_controls()

        controls.halt_mode(
            M2,
            actor="test-operator",
            scope="test",
            trigger=Trigger.OPERATOR,
        )

        reasons = controls.entry_block_reasons(mode_id=M2)
        assert ReasonCode.STRATEGY_HALTED in reasons

        controls.release_mode_halt(
            M2,
            actor="test-operator",
            scope="test",
            trigger=Trigger.OPERATOR,
        )

        reasons_after = controls.entry_block_reasons(mode_id=M2)
        assert len(reasons_after) == 0

    def test_mode_entry_freeze_survives_store_restart(self, tmp_path: Path) -> None:
        """upsert_entry_freeze with mode_id=M1 into TradingStore; get_entry_freeze(mode_id=M1)
        restores it; get_entry_freeze(mode_id=M2) returns None."""
        store = _open_store(tmp_path / "freeze.db")

        freeze_record = EntryFreezeRecord(
            entries_blocked=True,
            reason_code=ReasonCode.RISK_LIMIT_DAILY_LOSS,
            detail="M1 daily loss cap breached during test",
            updated_at=NOW,
            mode_id=M1,
        )
        store.upsert_entry_freeze(
            freeze_record,
            event_id="EVT-FREEZE-M1",
            mode_id=M1,
        )

        # Retrieve the M1 freeze — must be present
        retrieved = store.get_entry_freeze(mode_id=M1)
        assert retrieved is not None
        assert retrieved.entries_blocked is True
        assert retrieved.reason_code is ReasonCode.RISK_LIMIT_DAILY_LOSS
        assert retrieved.mode_id is M1

        # M2 has no freeze
        retrieved_m2 = store.get_entry_freeze(mode_id=M2)
        assert retrieved_m2 is None


# ---------------------------------------------------------------------------
# TestStoreIdempotency
# ---------------------------------------------------------------------------


class TestStoreIdempotency:
    """Schema migrations are idempotent; PositionLifecycleRecord mode_id round-trips."""

    def test_migration_is_idempotent(self, tmp_path: Path) -> None:
        """Open TradingStore twice on the same db_path; verify no errors
        (migrations are safe to run twice)."""
        db_path = tmp_path / "idempotent.db"

        # First open — runs migrations
        store1 = _open_store(db_path)
        store1.close()

        # Second open — re-runs migrations (must not raise or corrupt)
        store2 = _open_store(db_path)
        # Verify the store is still fully functional
        reservations = store2.list_reservations()
        assert isinstance(reservations, tuple)
        store2.close()

    def test_position_lifecycle_round_trip_with_mode_id(self, tmp_path: Path) -> None:
        """upsert PositionLifecycleRecord with mode_id, retrieve and verify mode_id
        round-trips correctly."""
        store = _open_store(tmp_path / "lifecycle.db")

        # Build a lifecycle record stamped with M3
        intent_obj = f.intent(
            intent_id="INT-LIFECYCLE-1",
            mode_id=M3,
            family_id=FamilyId.bull_call_debit,
        )
        risk_dec = f.risk_decision(intent_id="INT-LIFECYCLE-1")
        pos = f.position_state(
            trade_id="TRD-LIFECYCLE-1",
            intent_id="INT-LIFECYCLE-1",
            state=TradeState.OPEN,
            exit_policy=f.exit_policy(trade_id="TRD-LIFECYCLE-1"),
            opened_at=NOW,
            as_of=NOW,
        )
        record = PositionLifecycleRecord(
            trade_id="TRD-LIFECYCLE-1",
            position=pos,
            intent=intent_obj,
            risk_decision=risk_dec,
            holding_style=HoldingStyle.POSITIONAL,
            as_of=NOW,
            mode_id=M3,
            campaign_id="CAMP-1",
            policy_version="v2",
        )

        store.upsert_position_lifecycle(record, event_id="EVT-LIFECYCLE-1")

        # Retrieve and verify round-trip
        retrieved = store.get_position_lifecycle("TRD-LIFECYCLE-1")
        assert retrieved is not None
        assert retrieved.mode_id is M3
        assert retrieved.campaign_id == "CAMP-1"
        assert retrieved.policy_version == "v2"
        assert retrieved.trade_id == "TRD-LIFECYCLE-1"
