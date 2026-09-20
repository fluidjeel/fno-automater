"""POSTTRADE TradeAttribution + dual-entry journal (ADESK-B8)."""

from __future__ import annotations

import json
from hashlib import sha256

from pydantic import model_validator

from trading.domain.contracts.base import (
    ExactDecimal,
    NonEmptyStr,
    StrictBool,
    VersionedModel,
)
from trading.domain.contracts.improvement import ImprovementRecord
from trading.domain.enums import AttributionCode, ThesisVerdict

__all__ = [
    "TradeAttribution",
    "entry_journal_hash",
    "four_cell_verdict_counts",
    "verify_entry_journal_hash",
]


def entry_journal_hash(*, thesis_hash: str, narrative: str) -> str:
    """Immutable entry journal fingerprint (thesis hash + narrative)."""
    payload = json.dumps(
        {"thesis_hash": thesis_hash, "narrative": narrative},
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(payload.encode("utf-8")).hexdigest()


def verify_entry_journal_hash(
    *, stored_hash: str, thesis_hash: str, narrative: str
) -> bool:
    return stored_hash == entry_journal_hash(
        thesis_hash=thesis_hash, narrative=narrative
    )


class TradeAttribution(VersionedModel):
    trade_id: NonEmptyStr
    thesis_id: NonEmptyStr
    thesis_hash_verified: StrictBool
    outcome_r: ExactDecimal
    mae_r: ExactDecimal
    mfe_r: ExactDecimal
    capture_ratio: ExactDecimal
    thesis_verdict: ThesisVerdict
    invalidation_fired: tuple[NonEmptyStr, ...] = ()
    primary_attribution: AttributionCode
    execution_cost_r: ExactDecimal
    improvement_records: tuple[ImprovementRecord, ...] = ()
    narrative: NonEmptyStr
    entry_journal_hash: NonEmptyStr

    @model_validator(mode="after")
    def _hash_must_verify(self) -> TradeAttribution:
        if not self.thesis_hash_verified:
            raise ValueError("entry journal hash mismatch is a hard error")
        return self


def four_cell_verdict_counts(
    rows: tuple[TradeAttribution, ...],
) -> dict[str, int]:
    """Report all four teaching cells every week (plus UNTESTED)."""
    counts = {v.value: 0 for v in ThesisVerdict}
    for row in rows:
        counts[row.thesis_verdict.value] += 1
    return counts
