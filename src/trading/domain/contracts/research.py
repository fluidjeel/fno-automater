"""RESEARCH desk contracts (ADESK-E1 / E2).

Advisory only. Playbook edits and hypotheses never auto-implement; experiments
enter the existing SHADOW promotion ladder only.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import Field, model_validator

from trading.domain.contracts.base import (
    ExactDecimal,
    NonEmptyStr,
    StrictBool,
    StrictInt,
    StrictModel,
    UtcDatetime,
    VersionedModel,
)
from trading.domain.contracts.bias import BiasReport
from trading.domain.contracts.evaluation import ExperimentDefinition
from trading.domain.contracts.improvement import ImprovementCluster
from trading.domain.enums import (
    ExecutionMode,
    ImprovementArea,
    ImprovementStatus,
    PlaybookEditKind,
    PlaybookTriggerKind,
    TestabilityKind,
)

__all__ = [
    "ExperimentProposal",
    "PlaybookEditProposal",
    "ResearchHypothesis",
    "ResearchWeeklyReport",
]


class PlaybookEditProposal(StrictModel):
    """Structured TailPlaybook edit. Human signs; never auto-applied."""

    proposal_id: NonEmptyStr
    trigger: PlaybookTriggerKind
    edit_kind: PlaybookEditKind
    area: ImprovementArea
    source_cluster_id: NonEmptyStr | None = None
    source_bias_metric: NonEmptyStr | None = None
    auto_implement: StrictBool = False
    narrative: str = ""

    @model_validator(mode="after")
    def _never_auto_implement(self) -> PlaybookEditProposal:
        if self.auto_implement:
            raise ValueError("playbook edits must never auto-implement")
        return self


class ResearchWeeklyReport(VersionedModel):
    """Deterministic weekly RESEARCH artifact (PART 9 / Stage E1)."""

    week_id: NonEmptyStr
    as_of: UtcDatetime
    cohort_id: NonEmptyStr
    ranked_clusters: tuple[ImprovementCluster, ...]
    bias_report: BiasReport | None = None
    playbook_proposals: tuple[PlaybookEditProposal, ...] = ()
    record_count: StrictInt = Field(ge=0)
    cluster_count: StrictInt = Field(ge=0)
    attention_required: StrictBool = False
    narrative: str = ""


class ResearchHypothesis(VersionedModel):
    """Eligible cluster promoted to a hypothesis note (not yet an experiment)."""

    hypothesis_id: NonEmptyStr
    cluster_id: NonEmptyStr
    claim_key: NonEmptyStr
    area: ImprovementArea
    testable_as: TestabilityKind
    status: ImprovementStatus
    occurrences: StrictInt = Field(ge=1)
    estimated_cost_r_total: ExactDecimal = Field(default=Decimal("0"))
    supporting_record_ids: tuple[NonEmptyStr, ...]
    narrative: str = ""

    @model_validator(mode="after")
    def _status_is_promoted(self) -> ResearchHypothesis:
        if self.status is not ImprovementStatus.PROMOTED_TO_HYPOTHESIS:
            raise ValueError("ResearchHypothesis.status must be PROMOTED_TO_HYPOTHESIS")
        if self.testable_as is TestabilityKind.NOT_TESTABLE:
            raise ValueError("NOT_TESTABLE cannot become a hypothesis")
        return self


class ExperimentProposal(VersionedModel):
    """Hypothesis packaged into an existing-ladder SHADOW ExperimentDefinition."""

    proposal_id: NonEmptyStr
    hypothesis_id: NonEmptyStr
    experiment: ExperimentDefinition
    status: ImprovementStatus
    narrative: str = ""

    @model_validator(mode="after")
    def _shadow_gate(self) -> ExperimentProposal:
        if self.experiment.execution_mode is not ExecutionMode.SHADOW:
            raise ValueError(
                "ExperimentProposal must use ExecutionMode.SHADOW "
                "(existing promotion ladder entry)"
            )
        if not self.experiment.capital_limit.is_zero:
            raise ValueError("SHADOW ExperimentProposal requires capital_limit == 0")
        if self.status is not ImprovementStatus.PROMOTED_TO_HYPOTHESIS:
            raise ValueError("ExperimentProposal status must be PROMOTED_TO_HYPOTHESIS")
        return self


