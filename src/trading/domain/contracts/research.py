"""RESEARCH desk weekly contracts (ADESK-E1).

Advisory only. Playbook edits and clusters never auto-implement.
"""

from __future__ import annotations

from pydantic import Field, model_validator

from trading.domain.contracts.base import (
    NonEmptyStr,
    StrictBool,
    StrictInt,
    StrictModel,
    UtcDatetime,
    VersionedModel,
)
from trading.domain.contracts.bias import BiasReport
from trading.domain.contracts.improvement import ImprovementCluster
from trading.domain.enums import (
    ImprovementArea,
    PlaybookEditKind,
    PlaybookTriggerKind,
)

__all__ = [
    "PlaybookEditProposal",
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
