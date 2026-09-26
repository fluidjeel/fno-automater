"""Durable decision-and-reasoning records for every evaluation and exit."""

from __future__ import annotations

from pydantic import Field

from trading.domain.contracts.base import (
    ExactDecimal,
    NonEmptyStr,
    StrictBool,
    StrictInt,
    UtcDatetime,
    VersionedModel,
)
from trading.domain.enums import (
    DiscoveryDecisionKind,
    DiscoveryStage,
    ModeId,
    ReasonCode,
)
from trading.domain.primitives import Money, Price

__all__ = [
    "DiscoveryCandidateRow",
    "DiscoveryDecision",
    "DiscoveryDecisionInputs",
    "DiscoveryFillSnapshot",
    "DiscoverySizingSnapshot",
]


class DiscoveryDecisionInputs(VersionedModel):
    """Frozen market and data context at decision time."""

    spot: ExactDecimal | None = None
    trend: NonEmptyStr | None = None
    direction_fallback: StrictBool = False
    iv_bucket: NonEmptyStr | None = None
    event_state: NonEmptyStr | None = None
    quote_age_ms: StrictInt | None = Field(default=None, ge=0)
    return_15m: ExactDecimal | None = None
    return_60m: ExactDecimal | None = None
    trend_score: ExactDecimal | None = None


class DiscoveryCandidateRow(VersionedModel):
    """One bound or considered contract with selection outcome."""

    symbol: NonEmptyStr
    delta: ExactDecimal | None = Field(default=None, ge=-1, le=1)
    open_interest: StrictInt | None = Field(default=None, ge=0)
    spread_fraction: ExactDecimal | None = Field(default=None, ge=0)
    selected: StrictBool = False
    rejection_reason: ReasonCode | None = None


class DiscoverySizingSnapshot(VersionedModel):
    """Layer-2 sizing math frozen at approval time."""

    mode_equity: Money | None = None
    sizing_guide: Money | None = None
    one_lot_loss: Money | None = None
    lots: StrictInt | None = Field(default=None, ge=0)
    tags: tuple[NonEmptyStr, ...] = ()


class DiscoveryFillSnapshot(VersionedModel):
    """Paper fill assumption and strict shadow verdict."""

    assumed_price: Price | None = None
    strict_verdict: ReasonCode | None = None


class DiscoveryDecision(VersionedModel):
    """One durable record per (cycle, mode, family) evaluation or exit."""

    decision_id: NonEmptyStr
    cycle_id: NonEmptyStr
    as_of: UtcDatetime
    mode_id: ModeId
    family_id: NonEmptyStr
    strategy_id: NonEmptyStr
    experiment_id: NonEmptyStr
    decision: DiscoveryDecisionKind
    stage: DiscoveryStage
    reason_codes: tuple[ReasonCode, ...]
    reason_text: NonEmptyStr
    strict_would_block: tuple[ReasonCode, ...] = ()
    profile_version: NonEmptyStr
    code_version: NonEmptyStr
    inputs: DiscoveryDecisionInputs
    candidates: tuple[DiscoveryCandidateRow, ...] = ()
    sizing: DiscoverySizingSnapshot | None = None
    fill: DiscoveryFillSnapshot | None = None
    exit_rule: NonEmptyStr | None = None
    exit_price: Price | None = None
    realized_pnl: Money | None = None
