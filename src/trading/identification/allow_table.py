"""Deterministic NIFTY family allow-table from regime (before LLM advise)."""

from __future__ import annotations

from datetime import datetime, time
from decimal import Decimal
from enum import StrEnum
from zoneinfo import ZoneInfo

from trading.domain.contracts import MarketState
from trading.domain.contracts.advice import StructureChoice
from trading.domain.contracts.paper_data import PaperDataRequirements
from trading.identification.config import AllowRule, IdentificationPolicy
from trading.identification.p1_features import ObservedP1Features, blocked_families

__all__ = [
    "IvBucket",
    "SessionBucket",
    "allowed_families_for",
    "iv_bucket_for",
    "session_bucket_for",
]

_IST = ZoneInfo("Asia/Kolkata")
_NIFTY_EXCLUDED = frozenset({StructureChoice.COMMODITY_FUTURES_TREND.value})


class IvBucket(StrEnum):
    LOW = "LOW"
    MID = "MID"
    HIGH = "HIGH"
    UNKNOWN = "UNKNOWN"


class SessionBucket(StrEnum):
    AUCTION = "AUCTION"
    CONTINUOUS = "CONTINUOUS"
    CLOSED = "CLOSED"


def session_bucket_for(moment: datetime, policy: IdentificationPolicy) -> SessionBucket:
    """Map wall time to NSE session bucket using policy IST windows."""
    clock = moment.astimezone(_IST).time().replace(tzinfo=None)
    for window in policy.allow_table.auction_windows_ist:
        if _in_window(clock, _hhmm(window.start), _hhmm(window.end)):
            return SessionBucket.AUCTION
    cont = policy.allow_table.continuous_window_ist
    if _in_window(clock, _hhmm(cont.start), _hhmm(cont.end)):
        return SessionBucket.CONTINUOUS
    return SessionBucket.CLOSED


def iv_bucket_for(
    iv_percentile: Decimal | None, policy: IdentificationPolicy
) -> IvBucket:
    if iv_percentile is None:
        return IvBucket.UNKNOWN
    if iv_percentile <= policy.router.low_iv_percentile:
        return IvBucket.LOW
    if iv_percentile >= policy.allow_table.high_iv_percentile:
        return IvBucket.HIGH
    return IvBucket.MID


def allowed_families_for(
    market: MarketState,
    policy: IdentificationPolicy,
    *,
    p1: ObservedP1Features | None = None,
    paper_data: PaperDataRequirements | None = None,
) -> frozenset[str]:
    """Union of matching YAML rules, then hard NIFTY-route filters."""
    session = session_bucket_for(market.calculated_at, policy)
    iv_bucket = iv_bucket_for(market.iv_percentile, policy)
    trend = market.trend.value
    vol = market.volatility.value
    event = market.event_state

    allowed: set[str] = set()
    for rule in policy.allow_table.rules:
        if _match(
            rule,
            trend=trend,
            vol=vol,
            iv_bucket=iv_bucket.value,
            event=event,
            session=session.value,
        ):
            allowed.update(rule.allowed_families)

    families = frozenset(
        _apply_hard_filters(allowed, trend=trend, iv_bucket=iv_bucket, session=session)
    )
    if p1 is not None and paper_data is not None:
        return frozenset(families - blocked_families(p1, paper_data))
    return families


def _apply_hard_filters(
    families: set[str],
    *,
    trend: str,
    iv_bucket: IvBucket,
    session: SessionBucket,
) -> set[str]:
    out = set(families) - _NIFTY_EXCLUDED
    if not (iv_bucket is IvBucket.HIGH and trend == "RANGE"):
        out.discard(StructureChoice.DEFINED_RISK_MULTILEG.value)
    if session is not SessionBucket.AUCTION:
        out.discard(StructureChoice.CAS_MICROSTRUCTURE.value)
    return out


def _match(
    rule: AllowRule,
    *,
    trend: str,
    vol: str,
    iv_bucket: str,
    event: str,
    session: str,
) -> bool:
    return (
        _dim(rule.trend, trend)
        and _dim(rule.volatility, vol)
        and _dim(rule.iv_bucket, iv_bucket)
        and _dim(rule.event, event)
        and _dim(rule.session, session)
    )


def _dim(allowed: tuple[str, ...], value: str) -> bool:
    return "*" in allowed or value in allowed


def _in_window(clock: time, start: time, end: time) -> bool:
    """Half-open [start, end) on a single civil day."""
    if start <= end:
        return start <= clock < end
    return clock >= start or clock < end


def _hhmm(raw: str) -> time:
    hour_s, minute_s = raw.split(":")
    return time(hour=int(hour_s), minute=int(minute_s))
