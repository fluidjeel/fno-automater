"""Deterministic RESEARCH weekly runner (ADESK-E1 / PART 9).

Clusters ImprovementRecords, optionally runs the bias battery, and emits
structured playbook-edit proposals. No LLM. Never auto-implements.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from trading.analytics.bias import (
    BiasBatteryBands,
    DecisionBiasInput,
    TradeOutcomeInput,
    WarmColdPair,
    build_bias_report,
)
from trading.analytics.improvements import (
    apply_stale_status,
    cluster_improvements,
)
from trading.domain.contracts.bias import BiasMetricId, BiasReport
from trading.domain.contracts.improvement import ImprovementCluster, ImprovementRecord
from trading.domain.contracts.research import PlaybookEditProposal, ResearchWeeklyReport
from trading.domain.enums import (
    ImprovementArea,
    PlaybookEditKind,
    PlaybookTriggerKind,
)

__all__ = [
    "DEFAULT_AGENT_RUNS_DIR",
    "build_playbook_proposals",
    "build_research_weekly",
    "persist_research_weekly",
    "week_id_for",
]

DEFAULT_AGENT_RUNS_DIR = Path("data/agent_runs")

# Areas that map naturally to TailPlaybook maintenance (PART 8.3).
_PLAYBOOK_AREAS = frozenset(
    {
        ImprovementArea.RISK_LIMIT,
        ImprovementArea.PROCESS,
        ImprovementArea.EXIT_RULE,
        ImprovementArea.CORRELATION,
        ImprovementArea.DATA_GAP,
    }
)

_BIAS_TRIGGER_AREA: dict[BiasMetricId, ImprovementArea] = {
    BiasMetricId.DISPOSITION_EFFECT: ImprovementArea.EXIT_RULE,
    BiasMetricId.PREMATURE_EXIT: ImprovementArea.EXIT_RULE,
    BiasMetricId.RECENCY: ImprovementArea.SIZING,
    BiasMetricId.REVENGE: ImprovementArea.ENTRY_TIMING,
    BiasMetricId.OVERCONFIDENCE: ImprovementArea.SIZING,
    BiasMetricId.CONFIRMATION: ImprovementArea.THESIS_QUALITY,
    BiasMetricId.ANCHORING: ImprovementArea.PROCESS,
    BiasMetricId.FORM_STREAK: ImprovementArea.SIZING,
    BiasMetricId.HINDSIGHT_DRIFT: ImprovementArea.THESIS_QUALITY,
    BiasMetricId.SELECTION_DRIFT: ImprovementArea.ENTRY_TIMING,
    BiasMetricId.COST_BLINDNESS: ImprovementArea.COST,
}


def week_id_for(as_of: datetime) -> str:
    """ISO week id (UTC): YYYY-Www."""
    iso = as_of.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def build_playbook_proposals(
    clusters: Sequence[ImprovementCluster],
    bias_report: BiasReport | None,
) -> tuple[PlaybookEditProposal, ...]:
    """Map top playbook-relevant clusters + breached bias metrics to proposals."""
    proposals: list[PlaybookEditProposal] = []
    for cluster in clusters:
        if cluster.area not in _PLAYBOOK_AREAS:
            continue
        proposals.append(
            PlaybookEditProposal(
                proposal_id=f"pb:{cluster.cluster_id}",
                trigger=_trigger_for_area(cluster.area),
                edit_kind=PlaybookEditKind.REVIEW_ONLY,
                area=cluster.area,
                source_cluster_id=cluster.cluster_id,
                auto_implement=False,
                narrative=(
                    f"cluster {cluster.claim_key} occ={cluster.occurrences} "
                    f"cost_r={cluster.estimated_cost_r_total}"
                ),
            )
        )
    if bias_report is not None:
        for metric in bias_report.metrics:
            if not metric.band_breached:
                continue
            area = _BIAS_TRIGGER_AREA.get(metric.metric_id, ImprovementArea.PROCESS)
            proposals.append(
                PlaybookEditProposal(
                    proposal_id=f"pb:bias:{metric.metric_id.value}",
                    trigger=PlaybookTriggerKind.BIAS_BAND_BREACH,
                    edit_kind=PlaybookEditKind.TIGHTEN_THRESHOLD,
                    area=area,
                    source_bias_metric=metric.metric_id.value,
                    auto_implement=False,
                    narrative=f"bias band breached: {metric.metric_id.value}",
                )
            )
    return tuple(proposals)


def _trigger_for_area(area: ImprovementArea) -> PlaybookTriggerKind:
    if area is ImprovementArea.DATA_GAP:
        return PlaybookTriggerKind.FEED_QUALITY_DEGRADED
    if area is ImprovementArea.RISK_LIMIT:
        return PlaybookTriggerKind.TAIL_BUDGET_BREACH
    if area is ImprovementArea.CORRELATION:
        return PlaybookTriggerKind.SPREAD_WIDENING
    return PlaybookTriggerKind.VIX_JUMP


def build_research_weekly(
    records: Sequence[ImprovementRecord],
    *,
    as_of: datetime,
    cohort_id: str,
    trades: Sequence[TradeOutcomeInput] = (),
    decisions: Sequence[DecisionBiasInput] = (),
    warm_cold_pairs: Sequence[WarmColdPair] = (),
    bands: BiasBatteryBands | None = None,
    mark_stale: bool = True,
) -> ResearchWeeklyReport:
    """Build ranked clusters + optional bias summary + playbook proposals."""
    working: Sequence[ImprovementRecord] = records
    if mark_stale:
        working = apply_stale_status(working, as_of=as_of)
    clusters = cluster_improvements(working)
    bias_report: BiasReport | None = None
    if trades or decisions or warm_cold_pairs:
        bias_report = build_bias_report(
            as_of=as_of,
            cohort_id=cohort_id,
            trades=trades,
            decisions=decisions,
            warm_cold_pairs=warm_cold_pairs,
            bands=bands,
        )
    proposals = build_playbook_proposals(clusters, bias_report)
    attention = bool(bias_report.attention_required) if bias_report else False
    return ResearchWeeklyReport(
        week_id=week_id_for(as_of),
        as_of=as_of,
        cohort_id=cohort_id,
        ranked_clusters=clusters,
        bias_report=bias_report,
        playbook_proposals=proposals,
        record_count=len(working),
        cluster_count=len(clusters),
        attention_required=attention,
        narrative=(
            f"RESEARCH weekly {week_id_for(as_of)}: "
            f"{len(clusters)} clusters, {len(proposals)} playbook proposals"
        ),
    )


def persist_research_weekly(
    report: ResearchWeeklyReport,
    *,
    out_dir: Path | None = None,
) -> Path:
    """Write weekly JSON under data/agent_runs/ (or out_dir)."""
    root = out_dir if out_dir is not None else DEFAULT_AGENT_RUNS_DIR
    root.mkdir(parents=True, exist_ok=True)
    (root / ".gitkeep").touch(exist_ok=True)
    path = root / f"research_weekly_{report.week_id}.json"
    path.write_text(
        report.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )
    # Also write a tiny sidecar index for operators.
    index_path = root / "research_weekly_latest.json"
    index_path.write_text(
        json.dumps(
            {
                "week_id": report.week_id,
                "path": path.name,
                "cluster_count": report.cluster_count,
                "attention_required": report.attention_required,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return path
