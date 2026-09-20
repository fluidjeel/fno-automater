"""Deterministic StressReport builder (ADESK-A5 / AGENT_DESK_SPEC PART 8).

Pure arithmetic. assume_no_fills scenarios use defined_risk_max_loss (debit
spread → net debit). Shock scenarios apply linear greek approx, then clamp to
[-defined_risk_max_loss, +∞) for defined-risk structures.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from trading.domain.contracts.entry_freeze import EntryFreezeRecord
from trading.domain.contracts.stress import (
    FragilityFlag,
    ScenarioResult,
    StressReport,
    StressScenario,
    StressScenarioId,
)
from trading.domain.enums import ReasonCode
from trading.domain.primitives import Currency, Money

__all__ = [
    "DEFAULT_SCENARIOS",
    "PositionStressInput",
    "build_stress_entry_freeze",
    "build_stress_report",
    "evaluate_tail_budget",
]


@dataclass(frozen=True, slots=True)
class PositionStressInput:
    """Per-position inputs stress cannot invent from broker marks alone."""

    trade_id: str
    # Absolute max loss if stuck / stops do not fill (debit premium for debit
    # spreads). Must be non-negative.
    defined_risk_max_loss: Money
    # Approximate PnL for +1.0 spot percent and +1.0 IV percent.
    delta_pnl_per_pct: Money
    vega_pnl_per_pct: Money
    # False → shock PnL is not clamped (undefined risk).
    is_defined_risk: bool = True


DEFAULT_SCENARIOS: tuple[StressScenario, ...] = (
    StressScenario(
        scenario_id=StressScenarioId.GAP_DOWN_3,
        spot_shock_pct=Decimal("-3"),
        iv_shock_pct=Decimal("0"),
        assume_no_fills=False,
    ),
    StressScenario(
        scenario_id=StressScenarioId.GAP_DOWN_5,
        spot_shock_pct=Decimal("-5"),
        iv_shock_pct=Decimal("0"),
        assume_no_fills=False,
    ),
    StressScenario(
        scenario_id=StressScenarioId.GAP_DOWN_8,
        spot_shock_pct=Decimal("-8"),
        iv_shock_pct=Decimal("0"),
        assume_no_fills=False,
    ),
    StressScenario(
        scenario_id=StressScenarioId.GAP_UP_5,
        spot_shock_pct=Decimal("5"),
        iv_shock_pct=Decimal("0"),
        assume_no_fills=False,
    ),
    StressScenario(
        scenario_id=StressScenarioId.IV_SPIKE_50,
        spot_shock_pct=Decimal("0"),
        iv_shock_pct=Decimal("50"),
        assume_no_fills=False,
    ),
    StressScenario(
        scenario_id=StressScenarioId.IV_CRUSH_30,
        spot_shock_pct=Decimal("0"),
        iv_shock_pct=Decimal("-30"),
        assume_no_fills=False,
    ),
    StressScenario(
        scenario_id=StressScenarioId.GAP_DOWN_5_IV_SPIKE_50,
        spot_shock_pct=Decimal("-5"),
        iv_shock_pct=Decimal("50"),
        assume_no_fills=False,
    ),
    StressScenario(
        scenario_id=StressScenarioId.LIQUIDITY_EVAPORATION,
        spot_shock_pct=Decimal("-5"),
        iv_shock_pct=Decimal("30"),
        assume_no_fills=True,
    ),
    StressScenario(
        scenario_id=StressScenarioId.BROKER_OUTAGE_1_SESSION,
        spot_shock_pct=Decimal("-8"),
        iv_shock_pct=Decimal("50"),
        assume_no_fills=True,
    ),
)


def build_stress_report(
    *,
    as_of: datetime,
    snapshot_id: str,
    equity: Money,
    positions: Sequence[PositionStressInput],
    tail_budget_fraction: Decimal,
    scenarios: Sequence[StressScenario] = DEFAULT_SCENARIOS,
) -> StressReport:
    """Aggregate position stress into a versioned StressReport."""
    if equity.amount <= 0:
        raise ValueError("equity must be positive to compute stress percentages")
    if not scenarios:
        raise ValueError("at least one stress scenario is required")
    if tail_budget_fraction <= 0 or tail_budget_fraction > 1:
        raise ValueError("tail_budget_fraction must be in (0, 1]")

    results: list[ScenarioResult] = []
    for scenario in scenarios:
        pnl = _scenario_pnl(positions, scenario, currency=equity.currency)
        pct = (pnl.amount / equity.amount).quantize(Decimal("0.000001"))
        results.append(
            ScenarioResult(
                scenario_id=scenario.scenario_id,
                pnl=pnl,
                pnl_pct_of_equity=pct,
                assume_no_fills=scenario.assume_no_fills,
            )
        )

    worst = min(results, key=lambda r: r.pnl.amount)
    worst_pct = (worst.pnl.amount / equity.amount).quantize(Decimal("0.000001"))
    # Loss is negative PnL; breach when loss/equity exceeds budget.
    breached = (-worst.pnl.amount / equity.amount) > tail_budget_fraction

    flags: list[FragilityFlag] = []
    if breached:
        flags.append(FragilityFlag.TAIL_BUDGET_BREACHED)
    if any(not p.is_defined_risk for p in positions):
        flags.append(FragilityFlag.UNDEFINED_RISK_PRESENT)
    elif positions:
        flags.append(FragilityFlag.DEFINED_RISK_ONLY)
    if worst.assume_no_fills:
        flags.append(FragilityFlag.WORST_CASE_IS_NO_FILLS)

    return StressReport(
        as_of=as_of,
        snapshot_id=snapshot_id,
        equity=equity,
        results=tuple(results),
        worst_case=worst.pnl,
        worst_case_pct_equity=worst_pct,
        breached_budget=breached,
        fragility_flags=tuple(flags),
        tail_budget_fraction=tail_budget_fraction,
    )


def evaluate_tail_budget(report: StressReport) -> bool:
    """True when entries must freeze (budget breached)."""
    return report.breached_budget


def build_stress_entry_freeze(
    report: StressReport,
    *,
    now: datetime,
) -> EntryFreezeRecord | None:
    """EntryFreezeRecord when stress breaches the tail budget; else None."""
    if not report.breached_budget:
        return None
    return EntryFreezeRecord(
        entries_blocked=True,
        reason_code=ReasonCode.ENTRY_FROZEN,
        detail=(
            "tail budget breached: worst_case_pct_equity="
            f"{report.worst_case_pct_equity} "
            f"budget={report.tail_budget_fraction}"
        ),
        updated_at=now,
    )


def _scenario_pnl(
    positions: Sequence[PositionStressInput],
    scenario: StressScenario,
    *,
    currency: Currency,
) -> Money:
    total = Money.zero(currency)
    for pos in positions:
        if pos.defined_risk_max_loss.currency is not currency:
            raise ValueError(f"position {pos.trade_id} max_loss currency mismatch")
        if pos.defined_risk_max_loss.amount < 0:
            raise ValueError(
                f"position {pos.trade_id} defined_risk_max_loss must be >= 0"
            )
        if scenario.assume_no_fills:
            # Stuck for full defined loss (debit spread → net debit).
            leg = Money.of(-pos.defined_risk_max_loss.amount, currency)
        else:
            shock = (
                pos.delta_pnl_per_pct * scenario.spot_shock_pct
                + pos.vega_pnl_per_pct * scenario.iv_shock_pct
            )
            if pos.is_defined_risk:
                floor = Money.of(-pos.defined_risk_max_loss.amount, currency)
                leg = shock if shock.amount >= floor.amount else floor
            else:
                leg = shock
        total = total + leg
    return total
