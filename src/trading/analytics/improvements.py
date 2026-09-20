"""ImprovementRecord dedupe + clustering (ADESK-A7 / PART 9.2)."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal

from trading.domain.contracts.improvement import ImprovementCluster, ImprovementRecord
from trading.domain.enums import ImprovementArea, ImprovementStatus, TestabilityKind

__all__ = [
    "MIN_OCCURRENCES_FOR_HYPOTHESIS",
    "STALE_AFTER_DAYS",
    "apply_stale_status",
    "cluster_improvements",
    "hypothesis_eligible",
    "merge_duplicate",
    "normalize_claim_key",
]

MIN_OCCURRENCES_FOR_HYPOTHESIS = 3
STALE_AFTER_DAYS = 90
_WHITESPACE = re.compile(r"\s+")


def normalize_claim_key(area: ImprovementArea, claim: str) -> str:
    """Stable dedupe key: area + normalized claim text."""
    normalized = _WHITESPACE.sub(" ", claim.strip().lower())
    digest = hashlib.sha256(f"{area.value}|{normalized}".encode()).hexdigest()[:24]
    return f"{area.value}:{digest}"


def merge_duplicate(
    existing: ImprovementRecord,
    incoming: ImprovementRecord,
) -> ImprovementRecord:
    """Increment occurrences and union supporting trades on the same claim_key."""
    if existing.claim_key != incoming.claim_key:
        raise ValueError("cannot merge improvement records with different claim_key")
    if existing.area is not incoming.area:
        raise ValueError("cannot merge improvement records across areas")
    trades = tuple(
        dict.fromkeys((*existing.supporting_trade_ids, *incoming.supporting_trade_ids))
    )
    cost = max(existing.estimated_cost_r, incoming.estimated_cost_r)
    return existing.model_copy(
        update={
            "occurrences": existing.occurrences + 1,
            "supporting_trade_ids": trades,
            "estimated_cost_r": cost,
            "proposed_change": incoming.proposed_change or existing.proposed_change,
            "status": (
                ImprovementStatus.OPEN
                if existing.status is ImprovementStatus.STALE
                else existing.status
            ),
        }
    )


def cluster_improvements(
    records: Sequence[ImprovementRecord],
) -> tuple[ImprovementCluster, ...]:
    """Group by claim_key; rank by occurrences * estimated_cost_r (desc)."""
    buckets: dict[str, list[ImprovementRecord]] = {}
    for row in records:
        if row.status in {ImprovementStatus.REJECTED, ImprovementStatus.STALE}:
            continue
        buckets.setdefault(row.claim_key, []).append(row)

    clusters: list[ImprovementCluster] = []
    for claim_key, group in buckets.items():
        occurrences = sum(r.occurrences for r in group)
        cost_total = sum((r.estimated_cost_r for r in group), Decimal(0))
        score = Decimal(occurrences) * cost_total
        head = max(group, key=lambda r: (r.occurrences, r.opened_at))
        clusters.append(
            ImprovementCluster(
                cluster_id=f"cluster:{claim_key}",
                area=head.area,
                claim_key=claim_key,
                occurrences=occurrences,
                estimated_cost_r_total=cost_total,
                rank_score=score,
                record_ids=tuple(r.record_id for r in group),
                status=ImprovementStatus.CLUSTERED,
            )
        )
    clusters.sort(key=lambda c: (c.rank_score, c.occurrences), reverse=True)
    return tuple(clusters)


def hypothesis_eligible(
    record: ImprovementRecord,
    *,
    min_occurrences: int = MIN_OCCURRENCES_FOR_HYPOTHESIS,
) -> bool:
    """PART 9.2: promote only when recurrent and testable."""
    return (
        record.occurrences >= min_occurrences
        and record.testable_as is not TestabilityKind.NOT_TESTABLE
        and record.status
        not in {
            ImprovementStatus.REJECTED,
            ImprovementStatus.STALE,
            ImprovementStatus.IMPLEMENTED,
        }
    )


def apply_stale_status(
    records: Sequence[ImprovementRecord],
    *,
    as_of: datetime,
    stale_after_days: int = STALE_AFTER_DAYS,
) -> tuple[ImprovementRecord, ...]:
    """Mark singleton OPEN records older than N days as STALE."""
    cutoff = as_of - timedelta(days=stale_after_days)
    out: list[ImprovementRecord] = []
    for row in records:
        if (
            row.status is ImprovementStatus.OPEN
            and row.occurrences == 1
            and row.opened_at < cutoff
        ):
            out.append(row.model_copy(update={"status": ImprovementStatus.STALE}))
        else:
            out.append(row)
    return tuple(out)
