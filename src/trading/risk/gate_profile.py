"""DISCOVERY profile gate classification (DISC-A0 / DISCOVERY_MODE.md)."""

from __future__ import annotations

from enum import StrEnum, unique

from trading.config.discovery import DiscoveryConfig
from trading.domain.enums import EntryProfile, ReasonCode

__all__ = [
    "GateVerdict",
    "classify",
    "is_soft",
    "partition_reasons",
]


@unique
class GateVerdict(StrEnum):
    """Whether a reason code blocks new exposure."""

    HARD = "HARD"
    SOFT = "SOFT"


def classify(
    reason: ReasonCode,
    profile: EntryProfile,
    discovery: DiscoveryConfig | None = None,
) -> GateVerdict:
    """Return HARD under STRICT; DISCOVERY soft codes never reject."""
    if profile is EntryProfile.STRICT or discovery is None:
        return GateVerdict.HARD
    if reason in discovery.soft_reason_codes:
        return GateVerdict.SOFT
    return GateVerdict.HARD


def is_soft(
    reason: ReasonCode,
    profile: EntryProfile,
    discovery: DiscoveryConfig | None = None,
) -> bool:
    """True when the reason would be recorded but not block under DISCOVERY."""
    return classify(reason, profile, discovery) is GateVerdict.SOFT


def partition_reasons(
    reasons: tuple[ReasonCode, ...],
    profile: EntryProfile,
    discovery: DiscoveryConfig | None = None,
) -> tuple[tuple[ReasonCode, ...], tuple[ReasonCode, ...]]:
    """Split reason codes into hard blockers and strict_would_block shadows."""
    hard: list[ReasonCode] = []
    soft: list[ReasonCode] = []
    for reason in reasons:
        if reason is ReasonCode.OK:
            continue
        if is_soft(reason, profile, discovery):
            soft.append(reason)
        else:
            hard.append(reason)
    return tuple(dict.fromkeys(hard)), tuple(dict.fromkeys(soft))
