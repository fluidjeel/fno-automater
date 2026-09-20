"""ENTRY desk contracts: StrikeShortlist, StrikeCandidate, EntryAdvice (ADESK-B2).

SHADOW-only in Stage B. Validator rejects candidate_id not on the shortlist.
Nothing here places an order or mutates OMS/broker state.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Self

from pydantic import Field, model_validator

from trading.domain.contracts.advice import StructureChoice
from trading.domain.contracts.base import (
    ExactDecimal,
    NonEmptyStr,
    StrictInt,
    StrictModel,
    UtcDatetime,
    VersionedModel,
)
from trading.domain.contracts.common import ContractRef
from trading.domain.contracts.trade_thesis import TradeThesis
from trading.domain.enums import (
    AgentAction,
    EntryGateId,
    LiquidityGrade,
    Side,
    VetoCode,
)
from trading.domain.primitives import Money, Percent

__all__ = [
    "ALLOWED_ENTRY_ACTIONS",
    "EntryAdvice",
    "EntryAdviceError",
    "LegSpec",
    "StrikeCandidate",
    "StrikeShortlist",
    "assert_candidate_on_shortlist",
]

ALLOWED_ENTRY_ACTIONS: frozenset[AgentAction] = frozenset(
    {
        AgentAction.VETO_ENTRY,
        AgentAction.SELECT_STRIKE_CANDIDATE,
        AgentAction.ABSTAIN,
    }
)

_MIN_SHORTLIST = 2
_MAX_SHORTLIST = 6


class EntryAdviceError(ValueError):
    """Raised when EntryAdvice fails shortlist or action membership checks."""


class LegSpec(StrictModel):
    """One shortlist leg. Ratio only — L2 sizes lots."""

    leg_id: NonEmptyStr
    contract: ContractRef
    side: Side
    ratio: StrictInt = Field(ge=1)


class StrikeCandidate(StrictModel):
    """One risk-approved strike structure on the deterministic shortlist."""

    candidate_id: NonEmptyStr
    legs: tuple[LegSpec, ...] = Field(min_length=1)
    net_debit: Money
    max_loss: Money
    delta: ExactDecimal | None = None
    vega: ExactDecimal | None = None
    theta_per_day: ExactDecimal | None = None
    iv: ExactDecimal | None = None
    iv_percentile: ExactDecimal | None = None
    bid_ask_spread_pct: Percent
    observed_depth_lots: StrictInt | None = None
    breakeven_move_pct: Percent
    deterministic_score: ExactDecimal
    liquidity_grade: LiquidityGrade

    @model_validator(mode="after")
    def _same_currency(self) -> Self:
        if self.net_debit.currency != self.max_loss.currency:
            raise ValueError("net_debit and max_loss must share a currency")
        return self


class StrikeShortlist(VersionedModel):
    """Deterministic shortlist. Agent may only pick among candidates."""

    snapshot_id: NonEmptyStr
    underlying: NonEmptyStr
    expiry: date
    structure: StructureChoice
    candidates: tuple[StrikeCandidate, ...]
    shortlist_rule_version: NonEmptyStr

    @model_validator(mode="after")
    def _candidate_bounds_and_ids(self) -> Self:
        n = len(self.candidates)
        if n > _MAX_SHORTLIST:
            raise ValueError(
                f"StrikeShortlist allows at most {_MAX_SHORTLIST} candidates, got {n}"
            )
        ids = [c.candidate_id for c in self.candidates]
        if len(ids) != len(set(ids)):
            raise ValueError("StrikeShortlist candidate_id values must be unique")
        return self

    @property
    def eligible_for_agent(self) -> bool:
        """PART 4.2: skip agent when fewer than two candidates survive."""
        return len(self.candidates) >= _MIN_SHORTLIST

    def candidate_ids(self) -> frozenset[str]:
        return frozenset(c.candidate_id for c in self.candidates)

    def top_by_score(self) -> StrikeCandidate | None:
        if not self.candidates:
            return None
        return max(self.candidates, key=lambda c: c.deterministic_score)


class EntryAdvice(VersionedModel):
    """ENTRY desk output. SHADOW-logged only until Stage D."""

    as_of: UtcDatetime
    snapshot_id: NonEmptyStr
    action: AgentAction
    candidate_id: NonEmptyStr | None = None
    size_multiplier: ExactDecimal = Field(le=Decimal("1"), ge=Decimal("0"))
    thesis: TradeThesis | None = None
    veto_codes: tuple[VetoCode, ...] = ()
    evidence_ids: tuple[NonEmptyStr, ...] = ()
    failed_gate_ids: tuple[EntryGateId, ...] = ()
    narrative: NonEmptyStr
    agent_override: bool = False

    @model_validator(mode="after")
    def _action_coherence(self) -> Self:
        if self.action not in ALLOWED_ENTRY_ACTIONS:
            raise ValueError(
                f"EntryAdvice.action must be one of "
                f"{sorted(a.value for a in ALLOWED_ENTRY_ACTIONS)}; got {self.action}"
            )
        if self.action is AgentAction.SELECT_STRIKE_CANDIDATE:
            if self.candidate_id is None:
                raise ValueError("SELECT_STRIKE_CANDIDATE requires candidate_id")
            if self.thesis is None:
                raise ValueError("SELECT_STRIKE_CANDIDATE requires thesis")
            if self.veto_codes:
                raise ValueError("SELECT_STRIKE_CANDIDATE must not carry veto_codes")
        if self.action is AgentAction.VETO_ENTRY:
            if not self.veto_codes:
                raise ValueError("VETO_ENTRY requires at least one veto_code")
            if self.candidate_id is not None:
                raise ValueError("VETO_ENTRY must not name a candidate_id")
            if self.thesis is not None:
                raise ValueError("VETO_ENTRY must not carry a thesis")
        if self.action is AgentAction.ABSTAIN:
            if self.candidate_id is not None or self.thesis is not None:
                raise ValueError("ABSTAIN must not carry candidate_id or thesis")
            if self.veto_codes:
                raise ValueError("ABSTAIN must not carry veto_codes")
        return self


def assert_candidate_on_shortlist(
    advice: EntryAdvice, shortlist: StrikeShortlist
) -> None:
    """Reject SELECT_STRIKE_CANDIDATE whose candidate_id is not on the shortlist."""
    if advice.action is not AgentAction.SELECT_STRIKE_CANDIDATE:
        return
    if advice.candidate_id is None:
        raise EntryAdviceError("SELECT_STRIKE_CANDIDATE missing candidate_id")
    if advice.candidate_id not in shortlist.candidate_ids():
        raise EntryAdviceError(
            f"candidate_id {advice.candidate_id!r} is not on shortlist "
            f"for snapshot {shortlist.snapshot_id}"
        )
    if advice.snapshot_id != shortlist.snapshot_id:
        raise EntryAdviceError(
            f"advice snapshot_id {advice.snapshot_id!r} != "
            f"shortlist {shortlist.snapshot_id!r}"
        )
