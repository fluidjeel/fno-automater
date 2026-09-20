"""Daily stress / fragility accounting for Agent Desk FRAGILITY (ADESK-A5).

Deterministic only. PART 8: know what a tail costs; freeze entries if over budget.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum, unique

from pydantic import Field, model_validator

from trading.domain.contracts.base import (
    ExactDecimal,
    NonEmptyStr,
    StrictBool,
    StrictModel,
    UtcDatetime,
    VersionedModel,
)
from trading.domain.primitives import Money

__all__ = [
    "FragilityFlag",
    "ScenarioResult",
    "StressReport",
    "StressScenario",
    "StressScenarioId",
]


@unique
class StressScenarioId(StrEnum):
    """Closed set of book-level stress labels."""

    GAP_DOWN_3 = "GAP_DOWN_3"
    GAP_DOWN_5 = "GAP_DOWN_5"
    GAP_DOWN_8 = "GAP_DOWN_8"
    GAP_UP_5 = "GAP_UP_5"
    IV_SPIKE_50 = "IV_SPIKE_50"
    IV_CRUSH_30 = "IV_CRUSH_30"
    GAP_DOWN_5_IV_SPIKE_50 = "GAP_DOWN_5_IV_SPIKE_50"
    LIQUIDITY_EVAPORATION = "LIQUIDITY_EVAPORATION"
    BROKER_OUTAGE_1_SESSION = "BROKER_OUTAGE_1_SESSION"


class StressScenario(StrictModel):
    """One shock recipe. assume_no_fills is the honest exit assumption."""

    scenario_id: StressScenarioId
    spot_shock_pct: ExactDecimal
    iv_shock_pct: ExactDecimal
    assume_no_fills: StrictBool


class ScenarioResult(StrictModel):
    """PnL outcome of one scenario against the open book."""

    scenario_id: StressScenarioId
    pnl: Money
    pnl_pct_of_equity: ExactDecimal
    assume_no_fills: StrictBool


@unique
class FragilityFlag(StrEnum):
    """Machine-readable concentration / structure warnings from stress."""

    TAIL_BUDGET_BREACHED = "TAIL_BUDGET_BREACHED"
    DEFINED_RISK_ONLY = "DEFINED_RISK_ONLY"
    UNDEFINED_RISK_PRESENT = "UNDEFINED_RISK_PRESENT"
    WORST_CASE_IS_NO_FILLS = "WORST_CASE_IS_NO_FILLS"


class StressReport(VersionedModel):
    """Book-level tail cost. Built before entry and after close."""

    as_of: UtcDatetime
    snapshot_id: NonEmptyStr
    equity: Money
    results: tuple[ScenarioResult, ...]
    worst_case: Money
    worst_case_pct_equity: ExactDecimal
    breached_budget: StrictBool
    fragility_flags: tuple[FragilityFlag, ...] = ()
    tail_budget_fraction: ExactDecimal = Field(gt=Decimal(0), le=Decimal(1))

    @model_validator(mode="after")
    def _currency_and_worst_case(self) -> StressReport:
        currencies = {self.equity.currency, self.worst_case.currency}
        for row in self.results:
            currencies.add(row.pnl.currency)
        if len(currencies) != 1:
            raise ValueError(
                "StressReport mixes currencies "
                f"{sorted(c.value for c in currencies)}; convert before aggregating"
            )
        if not self.results:
            raise ValueError("StressReport requires at least one scenario result")
        most_negative = min(self.results, key=lambda r: r.pnl.amount)
        if self.worst_case.amount != most_negative.pnl.amount:
            raise ValueError(
                "worst_case must equal the minimum (most negative) scenario pnl"
            )
        return self
