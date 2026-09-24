"""Dual-expiry calendar settlement ledger (research only, Phase P14)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from trading.domain.enums import FamilyId, ReasonCode, Side
from trading.domain.primitives import Currency, Money
from trading.research.registry import is_calendar_family

__all__ = [
    "CalendarLegSettlement",
    "CalendarSettlementLedger",
    "CalendarSettlementRefusal",
    "build_calendar_settlement_ledger",
    "refuse_same_expiry_calendar_formula",
]

_UNSUPPORTED_PATHS: tuple[str, ...] = (
    "missed near-expiry exit before far-leg settlement",
    "far-leg adverse reversal after near expiry",
    "liquidity gap on the short near leg at expiry",
    "restart during dual-expiry settlement window",
)


@dataclass(frozen=True, slots=True)
class CalendarLegSettlement:
    """One calendar leg with its own expiry and settlement date."""

    symbol: str
    strike: Decimal
    expiry: date
    side: Side
    premium: Decimal


@dataclass(frozen=True, slots=True)
class CalendarSettlementRefusal:
    """Why a same-expiry payoff formula cannot bound calendar risk."""

    family_id: str
    reason_code: ReasonCode
    detail: str
    unsupported_paths: tuple[str, ...] = _UNSUPPORTED_PATHS


@dataclass(frozen=True, slots=True)
class CalendarSettlementLedger:
    """Research ledger for a dual-expiry calendar; not strict-book authoritative."""

    family_id: str
    near_leg: CalendarLegSettlement
    far_leg: CalendarLegSettlement
    initial_debit: Money
    same_expiry_formula_refused: bool
    finite_loss_proven: bool
    detail: str
    unsupported_paths: tuple[str, ...] = _UNSUPPORTED_PATHS


def refuse_same_expiry_calendar_formula(
    family_id: FamilyId | str,
) -> CalendarSettlementRefusal:
    """Refuse same-expiry vertical formulas for calendar families."""
    fam = family_id.value if isinstance(family_id, FamilyId) else str(family_id)
    if not is_calendar_family(fam):
        raise ValueError(f"{fam} is not a calendar family")
    return CalendarSettlementRefusal(
        family_id=fam,
        reason_code=ReasonCode.CALENDAR_SAME_EXPIRY_FORMULA_REFUSED,
        detail=(
            "same-expiry payoff formulas cannot bound dual-expiry calendar loss; "
            "use the settlement ledger instead"
        ),
    )


def build_calendar_settlement_ledger(
    *,
    family_id: FamilyId | str,
    near_leg: CalendarLegSettlement,
    far_leg: CalendarLegSettlement,
    lot_size: int,
    structure_lots: int = 1,
    currency: Currency = Currency.INR,
) -> CalendarSettlementLedger:
    """Record a calendar for research; finite loss remains unproven."""
    fam = family_id.value if isinstance(family_id, FamilyId) else str(family_id)
    if not is_calendar_family(fam):
        raise ValueError(f"{fam} is not a calendar family")
    if near_leg.expiry == far_leg.expiry:
        raise ValueError("calendar legs must have distinct expiries")
    if near_leg.strike != far_leg.strike:
        raise ValueError("calendar legs must share the same strike")
    if near_leg.side is not Side.SELL or far_leg.side is not Side.BUY:
        raise ValueError("calendar structure is short near / long far")
    contracts = Decimal(lot_size * structure_lots)
    debit = ((far_leg.premium - near_leg.premium) * contracts).quantize(Decimal("0.01"))
    initial_debit = Money.of(str(max(debit, Decimal("0"))), currency)
    return CalendarSettlementLedger(
        family_id=fam,
        near_leg=near_leg,
        far_leg=far_leg,
        initial_debit=initial_debit,
        same_expiry_formula_refused=True,
        finite_loss_proven=False,
        detail=(
            "initial debit is not a proof of bounded loss across two settlements; "
            "finite bound unproven for supported lifecycle paths"
        ),
    )
