"""Monthly meta-report for the Agent Desk (ADESK-E3 / PART 14.3).

Aggregates desk scorecards, demotions, and det-vs-desk comparison into one
operator-readable artifact (~10 minutes). Advisory only; never auto-implements.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from pydantic import Field, model_validator

from trading.analytics.agent_scorecard import AgentDeskScorecard
from trading.domain.contracts.base import (
    ExactDecimal,
    NonEmptyStr,
    StrictBool,
    StrictInt,
    StrictModel,
    UtcDatetime,
    VersionedModel,
)
from trading.domain.enums import AuthorityMode, DemotionReason, DeskRole

__all__ = [
    "DEFAULT_AGENT_RUNS_DIR",
    "DemotionEvent",
    "DetVsDeskRow",
    "MonthlyMetaReport",
    "build_monthly_meta_report",
    "month_id_for",
    "persist_monthly_meta_report",
    "render_monthly_meta_markdown",
]

DEFAULT_AGENT_RUNS_DIR = Path("data/agent_runs")


class DemotionEvent(StrictModel):
    """One desk demotion recorded for the monthly meta-report."""

    event_id: NonEmptyStr
    role: DeskRole
    at: UtcDatetime
    from_mode: AuthorityMode
    to_mode: AuthorityMode
    reason: DemotionReason
    narrative: str = ""

    @model_validator(mode="after")
    def _demotion_is_downgrade(self) -> DemotionEvent:
        order = (
            AuthorityMode.OBSERVE,
            AuthorityMode.SHADOW,
            AuthorityMode.ADVISORY,
            AuthorityMode.BOUNDED,
        )
        if order.index(self.to_mode) > order.index(self.from_mode):
            raise ValueError("demotion must not increase authority")
        return self


class DetVsDeskRow(StrictModel):
    """Deterministic-only vs Deterministic+Desk comparison for one role."""

    role: DeskRole
    sample_size: StrictInt = Field(ge=0)
    det_only_mean_r: ExactDecimal
    desk_mean_r: ExactDecimal
    delta_r: ExactDecimal
    narrative: str = ""


class MonthlyMetaReport(VersionedModel):
    """Operator-readable monthly meta-report (Stage E3)."""

    month_id: NonEmptyStr
    as_of: UtcDatetime
    desk_scorecards: tuple[AgentDeskScorecard, ...]
    demotions: tuple[DemotionEvent, ...]
    det_vs_desk: tuple[DetVsDeskRow, ...]
    total_decisions: StrictInt = Field(ge=0)
    demotion_count: StrictInt = Field(ge=0)
    attention_required: StrictBool = False
    narrative: str = ""


def month_id_for(as_of: datetime) -> str:
    """Calendar month id (UTC): YYYY-MM."""
    return f"{as_of.year:04d}-{as_of.month:02d}"


def build_monthly_meta_report(
    *,
    as_of: datetime,
    scorecards: Sequence[AgentDeskScorecard] = (),
    demotions: Sequence[DemotionEvent] = (),
    det_vs_desk: Sequence[DetVsDeskRow] = (),
) -> MonthlyMetaReport:
    """Aggregate desk scorecards, demotions, and det-vs-desk into one report."""
    cards = tuple(scorecards)
    dems = tuple(demotions)
    rows = tuple(det_vs_desk)
    total_decisions = sum(c.decisions for c in cards)
    # Attention when any demotion to OBSERVE for safety, or desk delta clearly negative.
    attention = any(
        d.to_mode is AuthorityMode.OBSERVE
        and d.reason is DemotionReason.SAFETY_INVARIANT
        for d in dems
    ) or any(r.delta_r < Decimal("0") and r.sample_size >= 20 for r in rows)
    narrative = (
        f"Monthly meta {month_id_for(as_of)}: {len(cards)} desk scorecards, "
        f"{len(dems)} demotions, {len(rows)} det-vs-desk rows, "
        f"{total_decisions} decisions"
    )
    return MonthlyMetaReport(
        month_id=month_id_for(as_of),
        as_of=as_of,
        desk_scorecards=cards,
        demotions=dems,
        det_vs_desk=rows,
        total_decisions=total_decisions,
        demotion_count=len(dems),
        attention_required=attention,
        narrative=narrative,
    )


def render_monthly_meta_markdown(report: MonthlyMetaReport) -> str:
    """Render an operator-readable markdown summary (~10 minutes)."""
    lines = [
        f"# Agent Desk monthly meta-report ({report.month_id})",
        "",
        f"As of: {report.as_of.isoformat()}",
        f"Attention required: {report.attention_required}",
        f"Total decisions: {report.total_decisions}",
        f"Demotions: {report.demotion_count}",
        "",
        "## Desk scorecards",
        "",
        "| Role | Decisions | Abstention | Override | Hallucination | Latency ms |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for card in report.desk_scorecards:
        lines.append(
            f"| {card.role.value} | {card.decisions} | "
            f"{card.abstention_rate} | {card.override_rate} | "
            f"{card.hallucination_rate} | {card.median_latency_ms} |"
        )
    lines.extend(["", "## Demotions", ""])
    if not report.demotions:
        lines.append("_None this month._")
    else:
        lines.append("| When | Role | From → To | Reason |")
        lines.append("| --- | --- | --- | --- |")
        for d in report.demotions:
            lines.append(
                f"| {d.at.isoformat()} | {d.role.value} | "
                f"{d.from_mode.value} → {d.to_mode.value} | {d.reason.value} |"
            )
    lines.extend(["", "## Deterministic vs Desk", ""])
    if not report.det_vs_desk:
        lines.append("_No comparison rows._")
    else:
        lines.append("| Role | N | Det-only R | Desk R | Δ R |")
        lines.append("| --- | ---: | ---: | ---: | ---: |")
        for r in report.det_vs_desk:
            lines.append(
                f"| {r.role.value} | {r.sample_size} | "
                f"{r.det_only_mean_r} | {r.desk_mean_r} | {r.delta_r} |"
            )
    lines.extend(["", f"_{report.narrative}_", ""])
    return "\n".join(lines)


def persist_monthly_meta_report(
    report: MonthlyMetaReport,
    *,
    out_dir: Path | None = None,
) -> Path:
    """Write JSON + markdown under data/agent_runs/ (or out_dir)."""
    root = out_dir if out_dir is not None else DEFAULT_AGENT_RUNS_DIR
    root.mkdir(parents=True, exist_ok=True)
    (root / ".gitkeep").touch(exist_ok=True)
    json_path = root / f"monthly_meta_{report.month_id}.json"
    json_path.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    md_path = root / f"monthly_meta_{report.month_id}.md"
    md_path.write_text(render_monthly_meta_markdown(report), encoding="utf-8")
    (root / "monthly_meta_latest.json").write_text(
        json.dumps(
            {
                "month_id": report.month_id,
                "json": json_path.name,
                "markdown": md_path.name,
                "attention_required": report.attention_required,
                "demotion_count": report.demotion_count,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return md_path
