"""Same-day exact-structure duplicate filtering for DISCOVERY PAPER."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from zoneinfo import ZoneInfo

from trading.domain.contracts.intent import TradeIntent
from trading.domain.contracts.lifecycle import PositionLifecycleRecord
from trading.domain.enums import ModeId, ReasonCode
from trading.portfolio.arbitration import extract_leg_signature

__all__ = [
    "StructureDuplicateKey",
    "StructureFilterResult",
    "filter_same_day_structure_duplicates",
    "index_same_day_structures",
    "structure_duplicate_key",
]

logger = logging.getLogger(__name__)

_IST = ZoneInfo("Asia/Kolkata")
_NEAR_DUPLICATE_DRIFT = Decimal("50")

StructureDuplicateKey = tuple[
    ModeId | None,
    str,
    str,
    str,
    str,
    tuple[tuple[str, str, int], ...],
]


def structure_duplicate_key(
    intent: TradeIntent,
    *,
    session_date: date,
) -> StructureDuplicateKey:
    """Build the same-day duplicate comparison key for one intent."""
    underlying, expiry_str, norm_legs = extract_leg_signature(intent)
    return (
        intent.mode_id,
        intent.strategy_id,
        underlying,
        expiry_str,
        session_date.isoformat(),
        norm_legs,
    )


def index_same_day_structures(
    lifecycles: Sequence[PositionLifecycleRecord],
    *,
    session_date: date,
    zone: ZoneInfo = _IST,
) -> dict[StructureDuplicateKey, str]:
    """Map same-day opened structures to their incumbent trade_id."""
    index: dict[StructureDuplicateKey, str] = {}
    for item in lifecycles:
        opened_at = item.position.opened_at
        if opened_at is None:
            continue
        if opened_at.astimezone(zone).date() != session_date:
            continue
        key = structure_duplicate_key(item.intent, session_date=session_date)
        index[key] = item.trade_id
    return index


@dataclass(frozen=True, slots=True)
class StructureFilterResult:
    """Intents kept after same-day duplicate filtering."""

    kept: tuple[TradeIntent, ...]
    rejections: tuple[tuple[TradeIntent, str, ReasonCode, str], ...]


def filter_same_day_structure_duplicates(
    intents: Sequence[TradeIntent],
    lifecycles: Sequence[PositionLifecycleRecord],
    *,
    session_date: date,
) -> StructureFilterResult:
    """Hard-suppress exact same-day duplicates; allow near-duplicates with logging."""
    index = index_same_day_structures(lifecycles, session_date=session_date)
    by_trade = {item.trade_id: item for item in lifecycles}
    kept: list[TradeIntent] = []
    rejections: list[tuple[TradeIntent, str, ReasonCode, str]] = []

    for intent in intents:
        key = structure_duplicate_key(intent, session_date=session_date)
        incumbent_id = index.get(key)
        if incumbent_id is not None:
            detail = (
                f"Same-day duplicate structure suppressed; incumbent: {incumbent_id}"
            )
            rejections.append(
                (intent, incumbent_id, ReasonCode.DUPLICATE_STRUCTURE, detail)
            )
            continue
        kept.append(intent)
        _log_near_duplicate(intent, index, by_trade, session_date=session_date)

    return StructureFilterResult(
        kept=tuple(kept),
        rejections=tuple(rejections),
    )


def _log_near_duplicate(
    intent: TradeIntent,
    index: dict[StructureDuplicateKey, str],
    lifecycles_by_trade: dict[str, PositionLifecycleRecord],
    *,
    session_date: date,
) -> None:
    for _key, trade_id in index.items():
        incumbent = lifecycles_by_trade.get(trade_id)
        if incumbent is None:
            continue
        drift = _uniform_strike_drift(intent, incumbent.intent)
        if drift is None or abs(drift) != _NEAR_DUPLICATE_DRIFT:
            continue
        if not _same_near_duplicate_shape(intent, incumbent.intent, session_date):
            continue
        logger.info(
            "near-duplicate structure allowed (ATM drift %s): "
            "candidate=%s incumbent=%s",
            drift,
            intent.intent_id,
            trade_id,
        )


def _same_near_duplicate_shape(
    candidate: TradeIntent,
    incumbent: TradeIntent,
    session_date: date,
) -> bool:
    candidate_key = structure_duplicate_key(candidate, session_date=session_date)
    incumbent_key = structure_duplicate_key(incumbent, session_date=session_date)
    return (
        candidate_key[0] == incumbent_key[0]
        and candidate_key[1] == incumbent_key[1]
        and candidate_key[2] == incumbent_key[2]
        and candidate_key[3] == incumbent_key[3]
        and candidate_key[4] == incumbent_key[4]
        and candidate_key[5] != incumbent_key[5]
    )


def _uniform_strike_drift(
    candidate: TradeIntent,
    incumbent: TradeIntent,
) -> Decimal | None:
    if len(candidate.legs) != len(incumbent.legs):
        return None
    candidate_rows = sorted(
        (
            leg.contract.strike,
            leg.side,
            leg.ratio,
            leg.contract.option_type,
        )
        for leg in candidate.legs
    )
    incumbent_rows = sorted(
        (
            leg.contract.strike,
            leg.side,
            leg.ratio,
            leg.contract.option_type,
        )
        for leg in incumbent.legs
    )
    paired: list[tuple[Decimal, Decimal]] = []
    for cand, inc in zip(candidate_rows, incumbent_rows, strict=True):
        if cand[1:] != inc[1:] or cand[0] is None or inc[0] is None:
            return None
        paired.append((cand[0], inc[0]))
    deltas = [left - right for left, right in paired]
    if len(set(deltas)) != 1:
        return None
    return deltas[0]
