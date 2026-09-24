"""Tests for Phase P3: Same-expiry payoff check for the four verticals.

Locks in:
- Scenario T03: Four verticals, multiple strikes/lot sizes: formula and generic payoff agree.
- Bounded loss, kink points, and tail slopes for all four verticals.
- Status IMPLEMENTED_UNIT for bull_call_debit, bear_put_debit, bull_put_credit, bear_call_credit.
- Scenario T08 & T09: LegSnapshotBundle distinct leg IDs and skew/staleness validation.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import NamedTuple

import pytest

import tests.factories as f
from tests.test_risk_gateway import option_snapshot
from trading.config.schema import FreshnessRules
from trading.domain.contracts.intent import IntentLeg
from trading.domain.contracts.snapshot import MarketQuote
from trading.domain.enums import (
    FamilyId,
    OptionType,
    ReasonCode,
    Side,
)
from trading.domain.primitives import Currency, Money
from trading.risk.payoff import (
    PayoffLeg,
    PayoffStatus,
    evaluate_same_expiry_payoff,
    formula_bear_call_credit,
    formula_bear_put_debit,
    formula_bull_call_debit,
    formula_bull_put_credit,
)
from trading.risk.snapshot_bundle import validate_leg_snapshot_bundle

NOW = datetime(2026, 9, 24, 4, 0, tzinfo=UTC)


def _inr(val: str) -> Money:
    return Money.of(val, Currency.INR)


class VerticalCase(NamedTuple):
    k1: Decimal
    k2: Decimal
    p1: Decimal
    p2: Decimal
    lot_size: int
    lots: int
    charges: Money


# ---------------------------------------------------------------------------
# 1. Bull Call Debit Payoff Tests (Scenario T03)
# ---------------------------------------------------------------------------


class TestBullCallDebitPayoff:
    @pytest.mark.parametrize(
        "case",
        [
            VerticalCase(
                Decimal("24000"),
                Decimal("24200"),
                Decimal("120"),
                Decimal("40"),
                25,
                1,
                _inr("0"),
            ),
            VerticalCase(
                Decimal("24000"),
                Decimal("24200"),
                Decimal("120"),
                Decimal("40"),
                50,
                2,
                _inr("100"),
            ),
            VerticalCase(
                Decimal("23500"),
                Decimal("24500"),
                Decimal("550"),
                Decimal("100"),
                75,
                5,
                _inr("250"),
            ),
            VerticalCase(
                Decimal("24100"),
                Decimal("24150"),
                Decimal("85.50"),
                Decimal("60.25"),
                25,
                3,
                _inr("50"),
            ),
        ],
    )
    def test_bull_call_debit_formula_and_generic_agree(
        self, case: VerticalCase
    ) -> None:
        legs = [
            PayoffLeg(
                strike=case.k1,
                option_type=OptionType.CALL,
                side=Side.BUY,
                premium=case.p1,
            ),
            PayoffLeg(
                strike=case.k2,
                option_type=OptionType.CALL,
                side=Side.SELL,
                premium=case.p2,
            ),
        ]
        report = evaluate_same_expiry_payoff(
            legs,
            lot_size=case.lot_size,
            structure_lots=case.lots,
            charges=case.charges,
            family_id=FamilyId.bull_call_debit,
        )

        assert report.status is PayoffStatus.IMPLEMENTED_UNIT
        assert report.formula_agrees is True
        assert report.is_bounded_loss is True
        assert report.is_bounded_profit is True
        assert report.upper_tail_slope == Decimal("0")
        assert report.lower_tail_slope == Decimal("0")

        exp_loss, exp_profit = formula_bull_call_debit(
            lower_strike=case.k1,
            higher_strike=case.k2,
            buy_premium=case.p1,
            sell_premium=case.p2,
            lot_size=case.lot_size,
            structure_lots=case.lots,
            charges=case.charges,
        )
        assert report.formula_max_loss == exp_loss
        assert report.generic_max_loss == exp_loss
        assert report.formula_max_profit == exp_profit
        assert report.generic_max_profit == exp_profit


# ---------------------------------------------------------------------------
# 2. Bear Put Debit Payoff Tests (Scenario T03)
# ---------------------------------------------------------------------------


class TestBearPutDebitPayoff:
    @pytest.mark.parametrize(
        "case",
        [
            VerticalCase(
                Decimal("23800"),
                Decimal("24000"),
                Decimal("110"),
                Decimal("35"),
                25,
                1,
                _inr("0"),
            ),
            VerticalCase(
                Decimal("23800"),
                Decimal("24000"),
                Decimal("110"),
                Decimal("35"),
                50,
                2,
                _inr("100"),
            ),
            VerticalCase(
                Decimal("23000"),
                Decimal("24000"),
                Decimal("520"),
                Decimal("80"),
                75,
                4,
                _inr("200"),
            ),
            VerticalCase(
                Decimal("24100"),
                Decimal("24200"),
                Decimal("95.75"),
                Decimal("50.25"),
                25,
                1,
                _inr("50"),
            ),
        ],
    )
    def test_bear_put_debit_formula_and_generic_agree(self, case: VerticalCase) -> None:
        # Buy K2 (higher strike), Sell K1 (lower strike)
        legs = [
            PayoffLeg(
                strike=case.k2,
                option_type=OptionType.PUT,
                side=Side.BUY,
                premium=case.p1,
            ),
            PayoffLeg(
                strike=case.k1,
                option_type=OptionType.PUT,
                side=Side.SELL,
                premium=case.p2,
            ),
        ]
        report = evaluate_same_expiry_payoff(
            legs,
            lot_size=case.lot_size,
            structure_lots=case.lots,
            charges=case.charges,
            family_id=FamilyId.bear_put_debit,
        )

        assert report.status is PayoffStatus.IMPLEMENTED_UNIT
        assert report.formula_agrees is True
        assert report.is_bounded_loss is True
        assert report.is_bounded_profit is True
        assert report.upper_tail_slope == Decimal("0")
        assert report.lower_tail_slope == Decimal("0")

        exp_loss, exp_profit = formula_bear_put_debit(
            lower_strike=case.k1,
            higher_strike=case.k2,
            buy_premium=case.p1,
            sell_premium=case.p2,
            lot_size=case.lot_size,
            structure_lots=case.lots,
            charges=case.charges,
        )
        assert report.formula_max_loss == exp_loss
        assert report.generic_max_loss == exp_loss
        assert report.formula_max_profit == exp_profit
        assert report.generic_max_profit == exp_profit


# ---------------------------------------------------------------------------
# 3. Bull Put Credit Payoff Tests (Scenario T03)
# ---------------------------------------------------------------------------


class TestBullPutCreditPayoff:
    @pytest.mark.parametrize(
        "case",
        [
            VerticalCase(
                Decimal("23800"),
                Decimal("24000"),
                Decimal("110"),
                Decimal("35"),
                25,
                1,
                _inr("0"),
            ),
            VerticalCase(
                Decimal("23800"),
                Decimal("24000"),
                Decimal("110"),
                Decimal("35"),
                50,
                2,
                _inr("100"),
            ),
            VerticalCase(
                Decimal("23000"),
                Decimal("24000"),
                Decimal("500"),
                Decimal("120"),
                75,
                3,
                _inr("150"),
            ),
            VerticalCase(
                Decimal("24100"),
                Decimal("24200"),
                Decimal("75.25"),
                Decimal("30.50"),
                25,
                1,
                _inr("25"),
            ),
        ],
    )
    def test_bull_put_credit_formula_and_generic_agree(
        self, case: VerticalCase
    ) -> None:
        # Sell K2 (higher strike), Buy K1 (lower strike)
        legs = [
            PayoffLeg(
                strike=case.k2,
                option_type=OptionType.PUT,
                side=Side.SELL,
                premium=case.p1,
            ),
            PayoffLeg(
                strike=case.k1,
                option_type=OptionType.PUT,
                side=Side.BUY,
                premium=case.p2,
            ),
        ]
        report = evaluate_same_expiry_payoff(
            legs,
            lot_size=case.lot_size,
            structure_lots=case.lots,
            charges=case.charges,
            family_id=FamilyId.bull_put_credit,
        )

        assert report.status is PayoffStatus.IMPLEMENTED_UNIT
        assert report.formula_agrees is True
        assert report.is_bounded_loss is True
        assert report.is_bounded_profit is True
        assert report.upper_tail_slope == Decimal("0")
        assert report.lower_tail_slope == Decimal("0")

        exp_loss, exp_profit = formula_bull_put_credit(
            lower_strike=case.k1,
            higher_strike=case.k2,
            sell_premium=case.p1,
            buy_premium=case.p2,
            lot_size=case.lot_size,
            structure_lots=case.lots,
            charges=case.charges,
        )
        assert report.formula_max_loss == exp_loss
        assert report.generic_max_loss == exp_loss
        assert report.formula_max_profit == exp_profit
        assert report.generic_max_profit == exp_profit


# ---------------------------------------------------------------------------
# 4. Bear Call Credit Payoff Tests (Scenario T03)
# ---------------------------------------------------------------------------


class TestBearCallCreditPayoff:
    @pytest.mark.parametrize(
        "case",
        [
            VerticalCase(
                Decimal("24000"),
                Decimal("24200"),
                Decimal("120"),
                Decimal("40"),
                25,
                1,
                _inr("0"),
            ),
            VerticalCase(
                Decimal("24000"),
                Decimal("24200"),
                Decimal("120"),
                Decimal("40"),
                50,
                2,
                _inr("100"),
            ),
            VerticalCase(
                Decimal("23500"),
                Decimal("24500"),
                Decimal("550"),
                Decimal("150"),
                75,
                4,
                _inr("200"),
            ),
            VerticalCase(
                Decimal("24100"),
                Decimal("24200"),
                Decimal("80.50"),
                Decimal("42.00"),
                25,
                1,
                _inr("50"),
            ),
        ],
    )
    def test_bear_call_credit_formula_and_generic_agree(
        self, case: VerticalCase
    ) -> None:
        # Sell K1 (lower strike), Buy K2 (higher strike)
        legs = [
            PayoffLeg(
                strike=case.k1,
                option_type=OptionType.CALL,
                side=Side.SELL,
                premium=case.p1,
            ),
            PayoffLeg(
                strike=case.k2,
                option_type=OptionType.CALL,
                side=Side.BUY,
                premium=case.p2,
            ),
        ]
        report = evaluate_same_expiry_payoff(
            legs,
            lot_size=case.lot_size,
            structure_lots=case.lots,
            charges=case.charges,
            family_id=FamilyId.bear_call_credit,
        )

        assert report.status is PayoffStatus.IMPLEMENTED_UNIT
        assert report.formula_agrees is True
        assert report.is_bounded_loss is True
        assert report.is_bounded_profit is True
        assert report.upper_tail_slope == Decimal("0")
        assert report.lower_tail_slope == Decimal("0")

        exp_loss, exp_profit = formula_bear_call_credit(
            lower_strike=case.k1,
            higher_strike=case.k2,
            sell_premium=case.p1,
            buy_premium=case.p2,
            lot_size=case.lot_size,
            structure_lots=case.lots,
            charges=case.charges,
        )
        assert report.formula_max_loss == exp_loss
        assert report.generic_max_loss == exp_loss
        assert report.formula_max_profit == exp_profit
        assert report.generic_max_profit == exp_profit


# ---------------------------------------------------------------------------
# 5. Unbounded and Invalid Geometries
# ---------------------------------------------------------------------------


class TestUnboundedAndInvalidGeometries:
    def test_naked_short_call_has_unbounded_loss(self) -> None:
        """A naked short call has upper_tail_slope = -1 and is rejected with UNBOUNDED_LOSS."""
        legs = [
            PayoffLeg(
                strike=Decimal("24000"),
                option_type=OptionType.CALL,
                side=Side.SELL,
                premium=Decimal("150"),
            ),
        ]
        report = evaluate_same_expiry_payoff(legs, lot_size=25, structure_lots=1)
        assert report.is_bounded_loss is False
        assert report.upper_tail_slope == Decimal("-1")
        assert report.status is PayoffStatus.UNBOUNDED_LOSS

    def test_naked_long_call_has_bounded_loss_and_unbounded_profit(self) -> None:
        """A naked long call has upper_tail_slope = +1, bounded loss = premium paid."""
        legs = [
            PayoffLeg(
                strike=Decimal("24000"),
                option_type=OptionType.CALL,
                side=Side.BUY,
                premium=Decimal("150"),
            ),
        ]
        report = evaluate_same_expiry_payoff(legs, lot_size=25, structure_lots=1)
        assert report.is_bounded_loss is True
        assert report.is_bounded_profit is False
        assert report.upper_tail_slope == Decimal("1")
        assert report.generic_max_profit is None
        # Max loss is 150 * 25 = 3,750
        assert report.generic_max_loss == _inr("3750")

    def test_invalid_formula_parameters_raise(self) -> None:
        """Invalid strikes or net debit raise ValueError in analytical formulas."""
        with pytest.raises(ValueError, match="higher_strike must be strictly greater"):
            formula_bull_call_debit(
                lower_strike=Decimal("24200"),
                higher_strike=Decimal("24000"),
                buy_premium=Decimal("120"),
                sell_premium=Decimal("40"),
                lot_size=25,
            )

        with pytest.raises(ValueError, match="requires net debit > 0"):
            formula_bull_call_debit(
                lower_strike=Decimal("24000"),
                higher_strike=Decimal("24200"),
                buy_premium=Decimal("40"),
                sell_premium=Decimal("120"),
                lot_size=25,
            )


# ---------------------------------------------------------------------------
# 6. Scenarios T08 & T09: Leg Snapshot Bundle Provenance & Freshness
# ---------------------------------------------------------------------------


class TestSnapshotBundleProvenance:
    @staticmethod
    def _make_freshness() -> FreshnessRules:
        return FreshnessRules(
            quote_max_age_ms=120000,
            max_leg_quote_skew_ms=2000,
            protection_stale_escalate_after_ms=120000,
            max_clock_drift_ms=500,
        )

    def test_t08_distinct_leg_ids_pass_and_are_preserved(self) -> None:
        """T08: distinct leg snapshot IDs in one multi-leg decision pass without overwriting."""
        t1 = NOW - timedelta(seconds=1)
        t2 = NOW - timedelta(milliseconds=800)

        contract1 = f.option_contract(
            symbol="NIFTY26SEP24000CE",
            strike=Decimal("24000"),
            option_type=OptionType.CALL,
        )
        contract2 = f.option_contract(
            symbol="NIFTY26SEP24200CE",
            strike=Decimal("24200"),
            option_type=OptionType.CALL,
        )

        intent = f.intent(
            intent_id="INT-MULTI-1",
            snapshot_id="SNAP-PARENT-ROOT",
            legs=(
                IntentLeg(leg_id="leg-1", contract=contract1, side=Side.BUY, ratio=1),
                IntentLeg(leg_id="leg-2", contract=contract2, side=Side.SELL, ratio=1),
            ),
        )

        # Each leg has its OWN distinct snapshot ID
        snap1 = option_snapshot(
            snapshot_id="SNAP-LEG-1-DISTINCT",
            contract=contract1,
            times=f.snapshot_times(
                event_time=t1,
                source_time=t1,
                receive_time=t1 + timedelta(milliseconds=10),
                calculation_time=t1 + timedelta(milliseconds=20),
            ),
            market=MarketQuote(bid=f.price("120.00"), ask=f.price("120.50")),
        )
        snap2 = option_snapshot(
            snapshot_id="SNAP-LEG-2-DISTINCT",
            contract=contract2,
            times=f.snapshot_times(
                event_time=t2,
                source_time=t2,
                receive_time=t2 + timedelta(milliseconds=10),
                calculation_time=t2 + timedelta(milliseconds=20),
            ),
            market=MarketQuote(bid=f.price("40.00"), ask=f.price("40.50")),
        )

        bundle = validate_leg_snapshot_bundle(
            intent,
            {"leg-1": snap1, "leg-2": snap2},
            now=NOW,
            freshness=self._make_freshness(),
        )

        assert bundle.reason is None
        assert bundle.decision_snapshot_id == "SNAP-PARENT-ROOT"
        assert len(bundle.leg_quotes) == 2
        # Distinct IDs were NOT overwritten
        assert bundle.leg_quotes[0].snapshot_id == "SNAP-LEG-1-DISTINCT"
        assert bundle.leg_quotes[1].snapshot_id == "SNAP-LEG-2-DISTINCT"
        assert bundle.leg_quotes[0].snapshot_id != bundle.leg_quotes[1].snapshot_id

    def test_t09_stale_or_skewed_leg_rejects(self) -> None:
        """T09: A stale leg or excessive timestamp skew between legs rejects."""
        contract1 = f.option_contract(
            symbol="NIFTY26SEP24000CE",
            strike=Decimal("24000"),
            option_type=OptionType.CALL,
        )
        contract2 = f.option_contract(
            symbol="NIFTY26SEP24200CE",
            strike=Decimal("24200"),
            option_type=OptionType.CALL,
        )

        intent = f.intent(
            intent_id="INT-MULTI-2",
            snapshot_id="SNAP-PARENT-2",
            legs=(
                IntentLeg(leg_id="leg-1", contract=contract1, side=Side.BUY, ratio=1),
                IntentLeg(leg_id="leg-2", contract=contract2, side=Side.SELL, ratio=1),
            ),
        )

        # Leg 2 timestamp skew exceeds max_leg_quote_skew_ms (2000 ms)
        t_fresh = NOW - timedelta(milliseconds=500)
        t_skewed = NOW - timedelta(milliseconds=3500)  # skew = 3000ms > 2000ms

        snap1 = option_snapshot(
            snapshot_id="SNAP-FRESH",
            contract=contract1,
            times=f.snapshot_times(
                event_time=t_fresh,
                source_time=t_fresh,
                receive_time=t_fresh + timedelta(milliseconds=10),
                calculation_time=t_fresh + timedelta(milliseconds=20),
            ),
            market=MarketQuote(bid=f.price("120.00"), ask=f.price("120.50")),
        )
        snap2 = option_snapshot(
            snapshot_id="SNAP-SKEWED",
            contract=contract2,
            times=f.snapshot_times(
                event_time=t_skewed,
                source_time=t_skewed,
                receive_time=t_skewed + timedelta(milliseconds=10),
                calculation_time=t_skewed + timedelta(milliseconds=20),
            ),
            market=MarketQuote(bid=f.price("40.00"), ask=f.price("40.50")),
        )

        bundle = validate_leg_snapshot_bundle(
            intent,
            {"leg-1": snap1, "leg-2": snap2},
            now=NOW,
            freshness=self._make_freshness(),
        )

        assert bundle.reason is ReasonCode.SNAPSHOT_MISMATCH
        # Provenance is still retained in the rejection audit
        assert bundle.leg_quotes[0].snapshot_id == "SNAP-FRESH"
        assert bundle.leg_quotes[1].snapshot_id == "SNAP-SKEWED"
