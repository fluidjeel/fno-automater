"""Hypothesis → SHADOW experiment via the existing promotion ladder (ADESK-E2).

Eligible clusters become ResearchHypothesis notes, then ExperimentProposal
wrapping ExperimentDefinition(ExecutionMode.SHADOW, capital_limit=0).
Ineligible clusters remain notes. Nothing auto-implements.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from trading.analytics.improvements import (
    MIN_OCCURRENCES_FOR_HYPOTHESIS,
    cluster_improvements,
    hypothesis_eligible,
)
from trading.domain.contracts.evaluation import ExperimentDefinition
from trading.domain.contracts.improvement import ImprovementCluster, ImprovementRecord
from trading.domain.contracts.research import ExperimentProposal, ResearchHypothesis
from trading.domain.enums import (
    ExecutionMode,
    ImprovementStatus,
    TestabilityKind,
)
from trading.domain.primitives import Currency, Money

__all__ = [
    "PromotionOutcome",
    "cluster_to_hypothesis",
    "hypothesis_to_shadow_experiment",
    "promote_improvements",
]


@dataclass(frozen=True, slots=True)
class PromotionOutcome:
    """Result of one promotion pass over improvement records."""

    clustered_records: tuple[ImprovementRecord, ...]
    clusters: tuple[ImprovementCluster, ...]
    hypotheses: tuple[ResearchHypothesis, ...]
    experiments: tuple[ExperimentProposal, ...]
    note_clusters: tuple[ImprovementCluster, ...]  # ineligible remain notes


def _records_by_id(
    records: Sequence[ImprovementRecord],
) -> dict[str, ImprovementRecord]:
    return {r.record_id: r for r in records}


def _cluster_eligible(
    cluster: ImprovementCluster,
    by_id: Mapping[str, ImprovementRecord],
    *,
    min_occurrences: int,
) -> tuple[bool, TestabilityKind | None]:
    """Eligible when cluster is recurrent and at least one source is testable."""
    if cluster.occurrences < min_occurrences:
        return False, None
    if cluster.status in {
        ImprovementStatus.REJECTED,
        ImprovementStatus.STALE,
        ImprovementStatus.IMPLEMENTED,
    }:
        return False, None
    testable: TestabilityKind | None = None
    for rid in cluster.record_ids:
        row = by_id.get(rid)
        if row is None:
            continue
        if row.testable_as is TestabilityKind.NOT_TESTABLE:
            continue
        if row.status in {
            ImprovementStatus.REJECTED,
            ImprovementStatus.STALE,
            ImprovementStatus.IMPLEMENTED,
        }:
            continue
        testable = row.testable_as
        break
    if testable is None:
        return False, None
    return True, testable


def cluster_to_hypothesis(
    cluster: ImprovementCluster,
    records: Sequence[ImprovementRecord],
    *,
    min_occurrences: int = MIN_OCCURRENCES_FOR_HYPOTHESIS,
) -> ResearchHypothesis | None:
    """Promote an eligible cluster; return None if it stays a note."""
    by_id = _records_by_id(records)
    ok, testable = _cluster_eligible(
        cluster, by_id, min_occurrences=min_occurrences
    )
    if not ok or testable is None:
        return None
    return ResearchHypothesis(
        hypothesis_id=f"hyp:{cluster.cluster_id}",
        cluster_id=cluster.cluster_id,
        claim_key=cluster.claim_key,
        area=cluster.area,
        testable_as=testable,
        status=ImprovementStatus.PROMOTED_TO_HYPOTHESIS,
        occurrences=cluster.occurrences,
        estimated_cost_r_total=cluster.estimated_cost_r_total,
        supporting_record_ids=cluster.record_ids,
        narrative=(
            f"promoted cluster {cluster.claim_key} "
            f"occ={cluster.occurrences} cost_r={cluster.estimated_cost_r_total}"
        ),
    )


def hypothesis_to_shadow_experiment(
    hypothesis: ResearchHypothesis,
    *,
    started_at: datetime,
    strategy_id: str = "research_hypothesis",
    strategy_version: str = "research-v1",
    parameter_version: str = "1",
    feature_set_version: str = "1",
    risk_policy_version: str = "1",
    fill_model_version: str = "conservative-v1",
    code_version: str = "0.1.0",
    currency: Currency = Currency.INR,
) -> ExperimentProposal:
    """Package hypothesis into existing-ladder SHADOW ExperimentDefinition."""
    experiment = ExperimentDefinition(
        experiment_id=f"exp:{hypothesis.hypothesis_id}",
        strategy_id=strategy_id,
        strategy_version=strategy_version,
        parameter_version=parameter_version,
        execution_mode=ExecutionMode.SHADOW,
        started_at=started_at,
        capital_limit=Money.zero(currency),
        parameters_frozen=True,
        feature_set_version=feature_set_version,
        risk_policy_version=risk_policy_version,
        fill_model_version=fill_model_version,
        code_version=code_version,
    )
    return ExperimentProposal(
        proposal_id=f"eprop:{hypothesis.hypothesis_id}",
        hypothesis_id=hypothesis.hypothesis_id,
        experiment=experiment,
        status=ImprovementStatus.PROMOTED_TO_HYPOTHESIS,
        narrative=(
            f"SHADOW experiment for {hypothesis.hypothesis_id}; "
            "capital_limit=0; not auto-implemented"
        ),
    )


def promote_improvements(
    records: Sequence[ImprovementRecord],
    *,
    as_of: datetime,
    min_occurrences: int = MIN_OCCURRENCES_FOR_HYPOTHESIS,
) -> PromotionOutcome:
    """OPEN → CLUSTERED → PROMOTED_TO_HYPOTHESIS for eligible; else remain notes.

    Emits SHADOW ExperimentProposals for each promoted hypothesis via the
    existing ExperimentDefinition gate (capital_limit must be 0).
    """
    clusters = cluster_improvements(records)
    clustered_ids = {rid for c in clusters for rid in c.record_ids}
    clustered_records: list[ImprovementRecord] = []
    for row in records:
        if row.record_id in clustered_ids and row.status is ImprovementStatus.OPEN:
            clustered_records.append(
                row.model_copy(update={"status": ImprovementStatus.CLUSTERED})
            )
        else:
            clustered_records.append(row)

    hypotheses: list[ResearchHypothesis] = []
    experiments: list[ExperimentProposal] = []
    notes: list[ImprovementCluster] = []
    promoted_record_ids: set[str] = set()
    occurrence_by_record: dict[str, int] = {}
    for cluster in clusters:
        for rid in cluster.record_ids:
            occurrence_by_record[rid] = max(
                occurrence_by_record.get(rid, 0), cluster.occurrences
            )

    for cluster in clusters:
        hyp = cluster_to_hypothesis(
            cluster, clustered_records, min_occurrences=min_occurrences
        )
        if hyp is None:
            notes.append(cluster)
            continue
        hypotheses.append(hyp)
        experiments.append(
            hypothesis_to_shadow_experiment(hyp, started_at=as_of)
        )
        promoted_record_ids.update(cluster.record_ids)

    final_records: list[ImprovementRecord] = []
    for row in clustered_records:
        if row.record_id not in promoted_record_ids:
            final_records.append(row)
            continue
        bumped = row.model_copy(
            update={
                "occurrences": max(
                    row.occurrences, occurrence_by_record.get(row.record_id, row.occurrences)
                )
            }
        )
        if hypothesis_eligible(bumped, min_occurrences=min_occurrences):
            final_records.append(
                bumped.model_copy(
                    update={"status": ImprovementStatus.PROMOTED_TO_HYPOTHESIS}
                )
            )
        else:
            final_records.append(row)

    return PromotionOutcome(
        clustered_records=tuple(final_records),
        clusters=clusters,
        hypotheses=tuple(hypotheses),
        experiments=tuple(experiments),
        note_clusters=tuple(notes),
    )
