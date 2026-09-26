"""Build and persist per-cycle paper observability evidence."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from trading.domain.contracts.cycle_evidence import (
    PaperCycleEvidence,
    StrategyCycleSummary,
)
from trading.runtime.activity_funnel import build_activity_funnel

if TYPE_CHECKING:
    from trading.runtime.paper_runner import PaperCycleResult

__all__ = ["build_cycle_evidence"]


def build_cycle_evidence(
    result: PaperCycleResult,
    *,
    cycle_id: str,
    as_of: datetime,
) -> PaperCycleEvidence:
    """Freeze one cycle's routing and strategy abstention lineage."""
    strategies = tuple(
        StrategyCycleSummary(
            strategy_id=outcome.strategy_id,
            snapshot_id=outcome.snapshot_id,
            execution_mode=outcome.execution_mode,
            executed=outcome.executed,
            rejection_reasons=outcome.rejection_reasons,
            rejection_details=tuple(outcome.rejection_details),
            entry_blocked_reasons=outcome.entry_blocked_reasons,
            intent_count=len(outcome.intents),
            setup_features=outcome.setup_features,
        )
        for outcome in result.outcomes
    )
    return PaperCycleEvidence(
        cycle_id=cycle_id,
        as_of=as_of,
        system_state=result.system_state,
        entries_blocked=result.entries_blocked,
        reconcile_id=result.reconcile_id,
        route_decision=result.route_decision,
        strategies=strategies,
        funnel=build_activity_funnel(result),
    )
