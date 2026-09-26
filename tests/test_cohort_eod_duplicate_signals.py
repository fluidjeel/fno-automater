"""Regression: EOD cohort must survive repeated snapshot IDs across cycles."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from trading.domain.contracts import CohortPackage
from trading.domain.enums import (
    FamilyId,
    ModeId,
    ReasonCode,
    SystemState,
)
from trading.domain.primitives import Currency, Money
from trading.runtime.cohort import persist_cohorts
from trading.runtime.paper_runner import PaperCycleResult, PaperStrategyOutcome

NOW = datetime(2026, 9, 25, 10, 0, tzinfo=UTC)
CAPITAL = Money.of("700000", Currency.INR)


def _declined_outcome(
    *,
    mode_id: ModeId,
    family_id: FamilyId,
    snapshot_id: str = "SNAP-REPEAT",
) -> PaperStrategyOutcome:
    return PaperStrategyOutcome(
        strategy_id="positional_long_option",
        snapshot_id=snapshot_id,
        intents=(),
        rejection_reasons=(ReasonCode.INSTRUMENT_UNKNOWN,),
        decisions=(),
        order_events=(),
        mode_id=mode_id,
        family_id=family_id,
    )


def test_persist_cohorts_unique_declined_signals_same_snapshot(tmp_path: Path) -> None:
    """Friday Oracle EOD crash: duplicate {snapshot_id}-blocked IDs in one package."""
    results = tuple(
        PaperCycleResult(
            system_state=SystemState.READY,
            outcomes=(
                _declined_outcome(
                    mode_id=ModeId.M2_DIRECTIONAL,
                    family_id=FamilyId.long_call,
                ),
                _declined_outcome(
                    mode_id=ModeId.M2_DIRECTIONAL,
                    family_id=FamilyId.long_put,
                ),
            ),
            reconcile_id="REC-1",
            entries_blocked=False,
        )
        for _ in range(3)
    )
    paths = persist_cohorts(
        results,
        output_dir=tmp_path,
        prefix="EXP-DISC",
        as_of=NOW,
        observation_start=NOW,
        capital_limit=CAPITAL,
        risk_policy_version="6",
        fill_model_version="touch-v1",
        code_version="1",
        feature_set_version="paper-session-v1",
        discovery_fingerprint="disc1",
    )
    assert len(paths) == 1
    package = CohortPackage.model_validate_json(paths[0].read_text(encoding="utf-8"))
    signal_ids = [signal.signal_id for signal in package.signals]
    assert len(signal_ids) == len(set(signal_ids))
    assert len(package.signals) == 6
