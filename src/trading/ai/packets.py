"""Versioned delta packets for Agent Desk (ADESK-A9 / PART 12.2).

The delta builder is load-bearing: whatever it omits, the agent cannot see.
Every tool fetch for a field the packet should have contained increments
`delta_gap_rate`. Prefix bytes before the cache boundary must stay stable.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
import json
from typing import Any

from trading.domain.contracts.base import (
    ExactDecimal,
    NonEmptyStr,
    StrictInt,
    UtcDatetime,
    VersionedModel,
)
from trading.domain.enums import DeskRole

__all__ = [
    "CACHE_BOUNDARY",
    "PACKET_VERSION",
    "DeltaGapCounter",
    "DeltaPacket",
    "InvalidationSnapshot",
    "PrefixSpec",
    "build_delta_packet",
    "build_prompt_prefix",
    "prefix_hash",
]

PACKET_VERSION = "1"
CACHE_BOUNDARY = "──── cache boundary ────"


class InvalidationSnapshot(VersionedModel):
    """One invalidation condition's live distance from breach."""

    condition_id: NonEmptyStr
    metric: NonEmptyStr
    status: NonEmptyStr  # HOLDING | BREACHED | UNKNOWN
    distance: ExactDecimal | None = None


class DeltaPacket(VersionedModel):
    """Role-scoped packet placed after the cache boundary."""

    packet_version: NonEmptyStr = PACKET_VERSION
    role: DeskRole
    as_of: UtcDatetime
    trade_id: NonEmptyStr | None = None
    snapshot_id: NonEmptyStr
    spot: ExactDecimal | None = None
    iv_percentile: ExactDecimal | None = None
    unrealized_r: ExactDecimal | None = None
    invalidations: tuple[InvalidationSnapshot, ...] = ()
    hard_risk: NonEmptyStr = "PASS"
    question: NonEmptyStr
    # Extra typed slots the role may need; keys are closed via allowlist.
    extras: Mapping[str, ExactDecimal | str | int | bool] = {}


@dataclass
class DeltaGapCounter:
    """Counts tool fetches for fields the packet should have supplied."""

    packet_fields: set[str]
    tool_fetches: list[str] = field(default_factory=list)

    def record_tool_fetch(self, field_name: str) -> None:
        self.tool_fetches.append(field_name)

    @property
    def gap_count(self) -> int:
        return sum(1 for name in self.tool_fetches if name in self.packet_fields)

    @property
    def fetch_count(self) -> int:
        return len(self.tool_fetches)

    @property
    def delta_gap_rate(self) -> Decimal:
        if self.fetch_count == 0:
            return Decimal("0")
        return (Decimal(self.gap_count) / Decimal(self.fetch_count)).quantize(
            Decimal("0.0001")
        )


@dataclass(frozen=True, slots=True)
class PrefixSpec:
    """Static prompt prefix segments before the cache boundary."""

    system_role: str
    authority_rules: str
    decision_policy: str
    tool_definitions: str
    output_schema: str


def build_prompt_prefix(spec: PrefixSpec) -> str:
    """Concatenate static prefix segments with the cache boundary marker."""
    parts = [
        spec.system_role.strip(),
        spec.authority_rules.strip(),
        spec.decision_policy.strip(),
        spec.tool_definitions.strip(),
        spec.output_schema.strip(),
        CACHE_BOUNDARY,
    ]
    return "\n\n".join(parts) + "\n"


def prefix_hash(prefix: str) -> str:
    """SHA-256 of the exact prefix bytes (UTF-8)."""
    return sha256(prefix.encode("utf-8")).hexdigest()


def build_delta_packet(
    *,
    role: DeskRole,
    as_of: datetime,
    snapshot_id: str,
    trade_id: str | None = None,
    spot: Decimal | None = None,
    iv_percentile: Decimal | None = None,
    unrealized_r: Decimal | None = None,
    invalidations: Sequence[InvalidationSnapshot] = (),
    hard_risk: str = "PASS",
    question: str | None = None,
    extras: Mapping[str, Any] | None = None,
) -> DeltaPacket:
    """Build a versioned delta packet for one desk call."""
    default_question = {
        DeskRole.ENTRY: "Does the evidence support a defined-risk entry now?",
        DeskRole.POSITION: (
            "Does anything here materially invalidate the frozen thesis?"
        ),
        DeskRole.PORTFOLIO: "Does shared fate require a size or entry veto?",
        DeskRole.MACRO: "Does this event change any open invalidation condition?",
        DeskRole.FRAGILITY: "Which named exposures drive the worst-case stress?",
        DeskRole.POSTTRADE: "Which attribution cell does this outcome belong to?",
        DeskRole.RESEARCH: "Which improvement clusters deserve a hypothesis?",
    }.get(role, "What does the evidence require?")
    return DeltaPacket(
        packet_version=PACKET_VERSION,
        role=role,
        as_of=as_of,
        trade_id=trade_id,
        snapshot_id=snapshot_id,
        spot=spot,
        iv_percentile=iv_percentile,
        unrealized_r=unrealized_r,
        invalidations=tuple(invalidations),
        hard_risk=hard_risk,
        question=question or default_question,
        extras=dict(extras or {}),
    )


def packet_to_canonical_json(packet: DeltaPacket) -> str:
    """Stable JSON for golden fixtures (sorted keys, no whitespace drift)."""
    return json.dumps(packet.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
