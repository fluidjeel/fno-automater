"""Phase P14: calendar research registry and dual-expiry settlement ledger."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

import tests.factories as f
from trading.domain.contracts import DerivativesContext, MarketState
from trading.domain.contracts.identification import (
    MacroStatus,
    TrendState,
    VolatilityState,
)
from trading.domain.enums import (
    DataQuality,
    ExecutionMode,
    FamilyId,
    FamilyResearchStatus,
    OptionType,
    ReasonCode,
    Side,
)
from trading.identification.binders import bind_long_call_calendar
from trading.identification.config import load_identification_policy
from trading.research.registry import (
    family_research_status,
    is_experimental_off_strict_book,
    refuses_same_expiry_payoff,
)
from trading.risk.calendar_settlement import (
    CalendarLegSettlement,
    build_calendar_settlement_ledger,
    refuse_same_expiry_calendar_formula,
)
from trading.risk.payoff import PayoffLeg, PayoffStatus, evaluate_same_expiry_payoff
from trading.runtime.paper_session import load_paper_session_config
from trading.runtime.startup_validation import (
    StartupValidationError,
    validate_startup_configuration,
)

ROOT = Path(__file__).resolve().parent.parent
POLICY = load_identification_policy(ROOT / "config" / "identification.yaml")


def _market() -> MarketState:
    return MarketState.model_validate(
        {
            "market_state_id": "market-p14",
            "feature_version": POLICY.feature_version,
            "calculated_at": f.NOW,
            "source_snapshot_ids": ("snapshot-1",),
            "trend": TrendState.RANGE,
            "volatility": VolatilityState.NORMAL,
            "event_state": "NORMAL",
            "macro_status": MacroStatus.NEUTRAL,
            "quality": DataQuality.VALID,
            "warmup_complete": True,
            "completed_bar_count": 60,
            "session_count": 20,
        }
    )


def _calendar_candidates() -> tuple[object, ...]:
    near = f.snapshot(
        contract=f.option_contract(
            symbol="NIFTY26SEP24000CE",
            expiry=date(2026, 9, 24),
        ),
        market=f.quote(bid=f.price("8.00"), ask=f.price("8.10")),
        derivatives=DerivativesContext(
            days_to_expiry=7,
            open_interest=5000,
            option_type=OptionType.CALL,
            underlying_price=f.price("24000"),
        ),
    )
    far = f.snapshot(
        contract=f.option_contract(
            symbol="NIFTY03OCT24000CE",
            expiry=date(2026, 10, 3),
        ),
        market=f.quote(bid=f.price("12.00"), ask=f.price("12.10")),
        derivatives=DerivativesContext(
            days_to_expiry=16,
            open_interest=5000,
            option_type=OptionType.CALL,
            underlying_price=f.price("24000"),
        ),
    )
    return (near, far)


class TestCalendarResearchRegistry:
    def test_calendar_status_is_experimental(self) -> None:
        status = family_research_status(FamilyId.long_call_calendar.value)
        assert status is FamilyResearchStatus.EXPERIMENTAL_ONLY_RISK_BOUND_UNPROVEN
        assert is_experimental_off_strict_book(FamilyId.long_put_calendar.value)

    def test_same_expiry_payoff_refused_for_calendar(self) -> None:
        assert refuses_same_expiry_payoff(FamilyId.long_call_calendar)
        refusal = refuse_same_expiry_calendar_formula(FamilyId.long_call_calendar)
        assert refusal.reason_code is ReasonCode.CALENDAR_SAME_EXPIRY_FORMULA_REFUSED
        report = evaluate_same_expiry_payoff(
            (
                PayoffLeg(
                    strike=Decimal("24000"),
                    option_type=OptionType.CALL,
                    side=Side.SELL,
                    premium=Decimal("8"),
                ),
                PayoffLeg(
                    strike=Decimal("24000"),
                    option_type=OptionType.CALL,
                    side=Side.BUY,
                    premium=Decimal("12"),
                ),
            ),
            lot_size=75,
            family_id=FamilyId.long_call_calendar,
        )
        assert report.status is PayoffStatus.DUAL_EXPIRY_REQUIRED
        assert report.formula_agrees is False

    def test_settlement_ledger_records_unproven_bound(self) -> None:
        ledger = build_calendar_settlement_ledger(
            family_id=FamilyId.long_call_calendar,
            near_leg=CalendarLegSettlement(
                symbol="NEAR",
                strike=Decimal("24000"),
                expiry=date(2026, 9, 24),
                side=Side.SELL,
                premium=Decimal("8"),
            ),
            far_leg=CalendarLegSettlement(
                symbol="FAR",
                strike=Decimal("24000"),
                expiry=date(2026, 10, 3),
                side=Side.BUY,
                premium=Decimal("12"),
            ),
            lot_size=75,
        )
        assert ledger.same_expiry_formula_refused is True
        assert ledger.finite_loss_proven is False

    def test_calendar_binder_selects_near_and_far_legs(self) -> None:
        bound = bind_long_call_calendar(
            _calendar_candidates(),
            market=_market(),
            policy=POLICY,
        )
        assert bound.binding.eligible is True
        assert len(bound.candidates) == 2
        assert bound.binding.selected_symbols == (
            "NIFTY26SEP24000CE",
            "NIFTY03OCT24000CE",
        )

    def test_startup_blocks_calendar_paper_stance(self) -> None:
        session_cfg = load_paper_session_config(ROOT / "config" / "paper_session.yaml")
        bad = session_cfg.model_copy(
            update={
                "strategy_stances": {
                    **session_cfg.strategy_stances,
                    FamilyId.long_call_calendar.value: ExecutionMode.PAPER,
                }
            }
        )
        with pytest.raises(StartupValidationError, match="EXPERIMENTAL"):
            validate_startup_configuration(bad)
