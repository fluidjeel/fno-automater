"""Quote-age freshness checks for Layer 3 strategies (DISC-A5)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from trading.config.discovery import DiscoveryConfig
from trading.config.schema import FreshnessRules
from trading.domain.contracts import FeatureSnapshot
from trading.domain.enums import EntryProfile, ReasonCode

if TYPE_CHECKING:
    from trading.strategies.base import StrategyContext

__all__ = [
    "QuoteFreshnessResult",
    "evaluate_quote_freshness",
    "freshest_quote_time",
    "quote_freshness_for_context",
    "quote_freshness_limits",
]


@dataclass(frozen=True, slots=True)
class QuoteFreshnessResult:
    """Outcome of a quote-age check for one strategy evaluation."""

    hard_reason: ReasonCode | None
    strict_would_block: tuple[ReasonCode, ...]
    detail: str | None


def freshest_quote_time(*snapshots: FeatureSnapshot) -> datetime:
    """Return the freshest quote or chain calculation time across snapshots."""
    if not snapshots:
        raise ValueError("at least one snapshot is required")
    return max(snapshot.times.calculation_time for snapshot in snapshots)


def quote_freshness_limits(
    *,
    entry_profile: EntryProfile,
    freshness: FreshnessRules,
    discovery_config: DiscoveryConfig | None,
    cas: bool = False,
) -> tuple[int, int | None]:
    """Return ``(strict_max_age_ms, hard_max_age_ms_or_none)`` for a profile."""
    if entry_profile is EntryProfile.DISCOVERY:
        if discovery_config is None:
            raise ValueError("discovery config is required for DISCOVERY profile")
        strict_ms = (
            discovery_config.cas_strict_quote_max_age_ms
            if cas
            else discovery_config.strict_quote_max_age_ms
        )
        return strict_ms, discovery_config.hard_quote_max_age_ms
    strict_ms = (
        freshness.require_cas_quote_max_age_ms()
        if cas
        else freshness.require_quote_max_age_ms()
    )
    return strict_ms, None


def evaluate_quote_freshness(
    *,
    now: datetime,
    snapshots: tuple[FeatureSnapshot, ...],
    entry_profile: EntryProfile,
    strict_quote_max_age_ms: int,
    hard_quote_max_age_ms: int | None,
) -> QuoteFreshnessResult:
    """Classify quote age using calculation time, never bar event time."""
    quote_time = freshest_quote_time(*snapshots)
    if now < quote_time:
        return QuoteFreshnessResult(
            hard_reason=ReasonCode.DATA_INVALID,
            strict_would_block=(),
            detail="decision instant precedes quote time",
        )
    age = now - quote_time
    age_ms = int(age / timedelta(milliseconds=1))
    if entry_profile is EntryProfile.STRICT:
        if age_ms > strict_quote_max_age_ms:
            return QuoteFreshnessResult(
                hard_reason=ReasonCode.DATA_STALE,
                strict_would_block=(),
                detail=(
                    f"quote age {age.total_seconds():.1f}s exceeds "
                    f"strict limit {strict_quote_max_age_ms / 1000:.0f}s"
                ),
            )
        return QuoteFreshnessResult(
            hard_reason=None, strict_would_block=(), detail=None
        )
    if hard_quote_max_age_ms is None:
        raise ValueError("hard_quote_max_age_ms is required for DISCOVERY profile")
    if age_ms > hard_quote_max_age_ms:
        return QuoteFreshnessResult(
            hard_reason=ReasonCode.DATA_STALE,
            strict_would_block=(),
            detail=(
                f"quote age {age.total_seconds():.1f}s exceeds "
                f"hard limit {hard_quote_max_age_ms / 1000:.0f}s"
            ),
        )
    if age_ms > strict_quote_max_age_ms:
        return QuoteFreshnessResult(
            hard_reason=None,
            strict_would_block=(ReasonCode.DATA_STALE,),
            detail=(
                f"quote age {age.total_seconds():.1f}s exceeds "
                f"strict limit {strict_quote_max_age_ms / 1000:.0f}s"
            ),
        )
    return QuoteFreshnessResult(hard_reason=None, strict_would_block=(), detail=None)


def quote_freshness_for_context(ctx: StrategyContext) -> QuoteFreshnessResult:
    """Evaluate quote freshness for a strategy context."""
    snapshots = (ctx.underlying, *ctx.candidates)
    return evaluate_quote_freshness(
        now=ctx.now,
        snapshots=snapshots,
        entry_profile=ctx.entry_profile,
        strict_quote_max_age_ms=ctx.strict_quote_max_age_ms,
        hard_quote_max_age_ms=ctx.hard_quote_max_age_ms,
    )
