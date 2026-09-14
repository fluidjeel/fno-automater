"""DATA_SPEC quality gate for Layer 1 snapshots."""

from __future__ import annotations

from datetime import datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

from trading.data.config import QualityConfig, SessionConfig
from trading.data.events import CanonicalMarketEvent
from trading.data.prices import (
    bar_close,
    chain_spot,
    positive_decimal,
    quote_last,
    resolve_last_price,
)
from trading.domain.contracts import DataQualityReport
from trading.domain.enums import DataQuality, ReasonCode

__all__ = [
    "assess_bar_snapshot",
    "assess_combined_snapshot",
    "assess_depth_snapshot",
    "assess_option_chain",
    "assess_quote_snapshot",
    "assess_tick",
    "in_session",
]


_STATE_RANK = {
    DataQuality.INVALID: 0,
    DataQuality.STALE: 1,
    DataQuality.DEGRADED: 2,
    DataQuality.VALID: 3,
}

# Two independent price readings are the minimum needed to cross-check sources.
_MIN_PRICES_FOR_CROSS_CHECK = 2


def _age_ms(now: datetime, event_time: datetime) -> int:
    return max(0, int((now - event_time).total_seconds() * 1000))


def _report(
    *,
    state: DataQuality,
    age_ms: int,
    warmup_complete: bool,
    source_status: str,
    reason_codes: tuple[ReasonCode, ...] = (),
    clock_drift_ms: int = 0,
    gap_count: int = 0,
) -> DataQualityReport:
    return DataQualityReport(
        state=state,
        age_ms=age_ms,
        warmup_complete=warmup_complete,
        source_status=source_status,
        reason_codes=reason_codes,
        clock_drift_ms=clock_drift_ms,
        gap_count=gap_count,
    )


def _report_for_age(
    *,
    age_ms: int,
    max_age_ms: int,
    warmup_complete: bool,
    source_status: str,
    empty: bool = False,
) -> DataQualityReport:
    if empty:
        return _report(
            state=DataQuality.INVALID,
            age_ms=age_ms,
            warmup_complete=warmup_complete,
            source_status=source_status,
            reason_codes=(ReasonCode.DATA_INVALID,),
        )
    if age_ms > max_age_ms:
        return _report(
            state=DataQuality.STALE,
            age_ms=age_ms,
            warmup_complete=warmup_complete,
            source_status=source_status,
            reason_codes=(ReasonCode.DATA_STALE,),
        )
    if age_ms > max_age_ms // 2:
        return _report(
            state=DataQuality.DEGRADED,
            age_ms=age_ms,
            warmup_complete=warmup_complete,
            source_status=source_status,
            reason_codes=(ReasonCode.DATA_DEGRADED,),
        )
    return _report(
        state=DataQuality.VALID,
        age_ms=age_ms,
        warmup_complete=warmup_complete,
        source_status=source_status,
    )


def assess_option_chain(
    event: CanonicalMarketEvent,
    *,
    now: datetime,
    max_age_ms: int,
    warmup_complete: bool = True,
) -> DataQualityReport:
    """Assign quality state from event age and payload completeness."""
    age_ms = _age_ms(now, event.event_time)
    strike_count = int(event.payload.get("strike_count", 0))
    return _report_for_age(
        age_ms=age_ms,
        max_age_ms=max_age_ms,
        warmup_complete=warmup_complete,
        source_status="empty_chain" if strike_count == 0 else "fyers",
        empty=strike_count == 0,
    )


def assess_quote_snapshot(
    event: CanonicalMarketEvent,
    *,
    now: datetime,
    max_age_ms: int,
    warmup_complete: bool = True,
) -> DataQualityReport:
    """Assign quality state for a quote snapshot."""
    age_ms = _age_ms(now, event.event_time)
    quote_count = int(event.payload.get("quote_count", 0))
    return _report_for_age(
        age_ms=age_ms,
        max_age_ms=max_age_ms,
        warmup_complete=warmup_complete,
        source_status="empty_quotes" if quote_count == 0 else "fyers",
        empty=quote_count == 0,
    )


def assess_bar_snapshot(
    event: CanonicalMarketEvent,
    *,
    now: datetime,
    max_age_ms: int,
    warmup_complete: bool = True,
) -> DataQualityReport:
    """Assign quality state for an OHLCV bar snapshot."""
    age_ms = _age_ms(now, event.receive_time)
    bar_count = int(event.payload.get("bar_count", 0))
    return _report_for_age(
        age_ms=age_ms,
        max_age_ms=max_age_ms,
        warmup_complete=warmup_complete,
        source_status="empty_bars" if bar_count == 0 else "fyers",
        empty=bar_count == 0,
    )


def assess_depth_snapshot(
    event: CanonicalMarketEvent,
    *,
    now: datetime,
    max_age_ms: int,
    warmup_complete: bool = True,
) -> DataQualityReport:
    """Assign quality state for a depth snapshot."""
    age_ms = _age_ms(now, event.receive_time)
    bid_count = int(event.payload.get("bid_count", 0))
    ask_count = int(event.payload.get("ask_count", 0))
    empty = bid_count == 0 or ask_count == 0
    if empty:
        return _report(
            state=DataQuality.DEGRADED,
            age_ms=age_ms,
            warmup_complete=warmup_complete,
            source_status="empty_depth",
            reason_codes=(ReasonCode.DEPTH_INSUFFICIENT,),
        )
    return _report_for_age(
        age_ms=age_ms,
        max_age_ms=max_age_ms,
        warmup_complete=warmup_complete,
        source_status="fyers",
    )


def assess_tick(
    event: CanonicalMarketEvent,
    *,
    now: datetime,
    max_age_ms: int,
    warmup_complete: bool = True,
) -> DataQualityReport:
    """Assign quality state for a websocket tick."""
    age_ms = _age_ms(now, event.event_time)
    ltp = event.payload.get("ltp")
    return _report_for_age(
        age_ms=age_ms,
        max_age_ms=max_age_ms,
        warmup_complete=warmup_complete,
        source_status="empty_tick" if ltp is None else "fyers",
        empty=ltp is None,
    )


def _merge(left: DataQualityReport, right: DataQualityReport) -> DataQualityReport:
    worse = left if _STATE_RANK[left.state] <= _STATE_RANK[right.state] else right
    reasons = tuple(dict.fromkeys((*left.reason_codes, *right.reason_codes)))
    warmup = left.warmup_complete and right.warmup_complete
    state = worse.state
    if state is DataQuality.VALID and not warmup:
        state = DataQuality.INVALID
        reasons = tuple(dict.fromkeys((*reasons, ReasonCode.WARMUP_INCOMPLETE)))
    if state is DataQuality.VALID and not reasons:
        reasons = ()
    return _report(
        state=state,
        age_ms=max(left.age_ms, right.age_ms),
        warmup_complete=warmup,
        source_status=worse.source_status,
        reason_codes=reasons,
        clock_drift_ms=max(left.clock_drift_ms, right.clock_drift_ms),
        gap_count=max(left.gap_count, right.gap_count),
    )


def _parse_hhmm(value: str) -> time:
    hour_s, minute_s = value.split(":", 1)
    return time(hour=int(hour_s), minute=int(minute_s))


def in_session(now: datetime, session: SessionConfig) -> bool:
    """True when now falls inside the configured exchange-local window."""
    if not session.verified or not session.open_local or not session.close_local:
        return False
    local = now.astimezone(ZoneInfo(session.timezone))
    start = _parse_hhmm(session.open_local)
    end = _parse_hhmm(session.close_local)
    return start <= local.time() <= end


def _quote_spread_bps(quote: CanonicalMarketEvent) -> Decimal | None:
    quotes = quote.payload.get("quotes", [])
    if not (isinstance(quotes, list) and quotes and isinstance(quotes[0], dict)):
        return None
    bid = positive_decimal(quotes[0].get("bid"))
    ask = positive_decimal(quotes[0].get("ask"))
    last = positive_decimal(quotes[0].get("lp", quotes[0].get("ltp")))
    if bid is None or ask is None or last is None or last == 0:
        return None
    return (ask - bid) / last * Decimal(10_000)


def _bps_gap(left: Decimal, right: Decimal) -> Decimal:
    base = left if left != 0 else right
    if base == 0:
        return Decimal(0)
    return abs(left - right) / base * Decimal(10_000)


def assess_combined_snapshot(
    *,
    chain: CanonicalMarketEvent,
    quote: CanonicalMarketEvent | None,
    bar: CanonicalMarketEvent | None,
    now: datetime,
    chain_max_age_ms: int,
    quote_max_age_ms: int,
    bar_max_age_ms: int,
    warmup_complete: bool = True,
    depth: CanonicalMarketEvent | None = None,
    market_status: CanonicalMarketEvent | None = None,
    depth_max_age_ms: int = 60_000,
    session: SessionConfig | None = None,
    quality_config: QualityConfig | None = None,
) -> DataQualityReport:
    """Combine quality from required chain and optional live feeds."""
    cfg = quality_config or QualityConfig()
    strike_count = int(chain.payload.get("strike_count", 0))
    bar_count = int(bar.payload.get("bar_count", 0)) if bar is not None else 0
    warmup = (
        warmup_complete
        and strike_count >= cfg.min_strike_count
        and (bar is None or bar_count >= cfg.min_bar_count)
    )
    quality = assess_option_chain(
        chain,
        now=now,
        max_age_ms=chain_max_age_ms,
        warmup_complete=True,
    )
    if resolve_last_price(chain=chain, quote=quote, bar=bar) is None:
        quality = _merge(
            quality,
            _report(
                state=DataQuality.INVALID,
                age_ms=quality.age_ms,
                warmup_complete=warmup,
                source_status="price_unavailable",
                reason_codes=(ReasonCode.PRICE_UNAVAILABLE,),
            ),
        )
    if session is not None and not session.verified:
        quality = _merge(
            quality,
            _report(
                state=DataQuality.INVALID,
                age_ms=quality.age_ms,
                warmup_complete=False,
                source_status="session_unverified",
                reason_codes=(ReasonCode.CONFIG_UNVERIFIED, ReasonCode.OUTSIDE_SESSION),
            ),
        )
    elif session is not None and not in_session(now, session):
        quality = _merge(
            quality,
            _report(
                state=DataQuality.INVALID,
                age_ms=quality.age_ms,
                warmup_complete=warmup,
                source_status="outside_session",
                reason_codes=(ReasonCode.OUTSIDE_SESSION,),
            ),
        )
    if not warmup:
        quality = _merge(
            quality,
            _report(
                state=DataQuality.INVALID,
                age_ms=quality.age_ms,
                warmup_complete=False,
                source_status="warmup",
                reason_codes=(ReasonCode.WARMUP_INCOMPLETE,),
            ),
        )
    drift = max(0, int((chain.receive_time - chain.event_time).total_seconds() * 1000))
    if drift > cfg.max_clock_drift_ms:
        drift_state = (
            DataQuality.INVALID if cfg.clock_drift_invalid else DataQuality.DEGRADED
        )
        quality = _merge(
            quality,
            _report(
                state=drift_state,
                age_ms=quality.age_ms,
                warmup_complete=warmup,
                source_status="clock_drift",
                reason_codes=(ReasonCode.CLOCK_DRIFT,),
                clock_drift_ms=drift,
            ),
        )
    else:
        quality = _report(
            state=quality.state,
            age_ms=quality.age_ms,
            warmup_complete=quality.warmup_complete,
            source_status=quality.source_status,
            reason_codes=quality.reason_codes,
            clock_drift_ms=drift,
            gap_count=quality.gap_count,
        )
    if quote is not None:
        quote_quality = assess_quote_snapshot(
            quote,
            now=now,
            max_age_ms=quote_max_age_ms,
            warmup_complete=True,
        )
        if quote_quality.state is DataQuality.INVALID:
            quote_quality = _report(
                state=DataQuality.DEGRADED,
                age_ms=quote_quality.age_ms,
                warmup_complete=warmup,
                source_status="missing_quotes",
                reason_codes=(ReasonCode.DATA_DEGRADED,),
            )
        quality = _merge(quality, quote_quality)
        spread_bps = _quote_spread_bps(quote)
        if spread_bps is not None and spread_bps > cfg.max_spread_bps:
            quality = _merge(
                quality,
                _report(
                    state=DataQuality.DEGRADED,
                    age_ms=quality.age_ms,
                    warmup_complete=warmup,
                    source_status="wide_spread",
                    reason_codes=(ReasonCode.SPREAD_TOO_WIDE,),
                ),
            )
    if bar is not None:
        bar_quality = assess_bar_snapshot(
            bar,
            now=now,
            max_age_ms=bar_max_age_ms,
            warmup_complete=True,
        )
        if bar_quality.state is DataQuality.INVALID:
            bar_quality = _report(
                state=DataQuality.DEGRADED,
                age_ms=bar_quality.age_ms,
                warmup_complete=warmup,
                source_status="missing_bars",
                reason_codes=(ReasonCode.DATA_DEGRADED,),
            )
        quality = _merge(quality, bar_quality)
    if depth is not None:
        quality = _merge(
            quality,
            assess_depth_snapshot(
                depth,
                now=now,
                max_age_ms=depth_max_age_ms,
                warmup_complete=True,
            ),
        )
    if market_status is not None and session is not None and in_session(now, session):
        status = str(market_status.payload.get("status") or "").upper()
        if status and status != "OPEN":
            quality = _merge(
                quality,
                _report(
                    state=DataQuality.DEGRADED,
                    age_ms=quality.age_ms,
                    warmup_complete=warmup,
                    source_status="market_closed",
                    reason_codes=(ReasonCode.DATA_DEGRADED,),
                ),
            )
    prices: list[Decimal] = []
    spot = chain_spot(chain)
    if spot is not None:
        prices.append(spot)
    if quote is not None:
        last = quote_last(quote)
        if last is not None:
            prices.append(last)
    if bar is not None:
        close = bar_close(bar)
        if close is not None:
            prices.append(close)
    if len(prices) >= _MIN_PRICES_FOR_CROSS_CHECK:
        widest = max(
            _bps_gap(prices[i], prices[j])
            for i in range(len(prices))
            for j in range(i + 1, len(prices))
        )
        if widest > cfg.max_cross_source_bps:
            quality = _merge(
                quality,
                _report(
                    state=DataQuality.DEGRADED,
                    age_ms=quality.age_ms,
                    warmup_complete=warmup,
                    source_status="cross_source",
                    reason_codes=(ReasonCode.SNAPSHOT_MISMATCH,),
                ),
            )
    return quality
