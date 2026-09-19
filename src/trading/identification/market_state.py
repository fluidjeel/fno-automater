"""Point-in-time NIFTY regime calculation from completed five-minute bars.

IV regime fields (`iv_percentile`, `iv_rv_ratio`) use India VIX history when
provided; missing VIX is fail-visible via ReasonCode.DATA_GAP (no invented levels).
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from statistics import median
from zoneinfo import ZoneInfo

from trading.data.events import CanonicalMarketEvent
from trading.domain.contracts import FeatureSnapshot, MarketState
from trading.domain.contracts.identification import (
    MacroStatus,
    TrendState,
    VolatilityState,
)
from trading.domain.enums import ReasonCode
from trading.identification.config import IdentificationPolicy
from trading.news.contracts import EventRiskState
from trading.strategies.macro import MacroAssessment, MacroBias, accepted_macro_bias

__all__ = ["build_market_state"]

_IST = ZoneInfo("Asia/Kolkata")
_BAR_SECONDS = 300
_ANNUAL_BARS = Decimal(75 * 252)
# P1 presence for this series is gated by config/paper_data.yaml windows
# (short 12 / long 50 completed 5m bars; 75 session bars * 252 days).
_ATR_PERIOD = 14
_RETURN_60_BARS = 13
_MIN_STDEV_SAMPLES = 2
_ZERO = Decimal(0)
_ONE = Decimal(1)
_Bar = tuple[datetime, Decimal, Decimal, Decimal, Decimal, Decimal]


def build_market_state(
    bar_event: CanonicalMarketEvent | None,
    *,
    underlying: FeatureSnapshot,
    option_candidates: Sequence[FeatureSnapshot],
    event_risk: EventRiskState | None,
    macro: MacroAssessment | None,
    as_of: datetime,
    policy: IdentificationPolicy,
    iv_history: Sequence[Decimal] = (),
    vix_history: Sequence[Decimal] = (),
) -> MarketState:
    bars = _completed_bars(bar_event, as_of)
    sessions = {row[0].astimezone(_IST).date() for row in bars}
    source_ids = (underlying.snapshot_id,) + (
        () if bar_event is None else (bar_event.event_id,)
    )
    reasons: list[ReasonCode] = []
    warm = (
        len(bars) >= policy.warmup.min_completed_bars
        and len(sessions) >= policy.warmup.min_sessions
    )
    if not warm:
        reasons.append(ReasonCode.WARMUP_INCOMPLETE)

    values = _features(bars)
    # India VIX history is authoritative for iv_percentile / iv_rv_ratio.
    # iv_history remains as a legacy alias for tests; vix_history wins when set.
    vix_series = tuple(vix_history) if vix_history else tuple(iv_history)
    current_vix = vix_series[-1] if vix_series else None
    if current_vix is None:
        # Spot VIX from option-chain payload when history has not warmed yet.
        spot = underlying.features.get("india_vix")
        if spot is not None and spot > 0:
            current_vix = spot
    min_sessions = policy.warmup.min_sessions
    if len(vix_series) < min_sessions:
        iv_percentile = None
        reasons.append(ReasonCode.DATA_GAP)
        warm = False
    else:
        iv_percentile = _percentile(current_vix, vix_series)
    rv = values.get("annualized_rv")
    iv_rv = None
    if current_vix is not None and rv is not None and rv > 0:
        iv_rv = _q(current_vix / rv)
    if iv_percentile is None or iv_rv is None:
        if ReasonCode.DATA_GAP not in reasons:
            reasons.append(ReasonCode.DATA_GAP)
        warm = False

    trend_score = _trend_score(values)
    trend = _trend(values, trend_score, warm, policy)
    volatility = _volatility(values.get("rv_ratio"), warm, policy)
    macro_status = _macro_status(macro, trend, as_of)
    quality = underlying.quality.state
    if quality.blocks_new_exposure:
        reasons.append(ReasonCode.DATA_INVALID)
    event_state = "MISSING" if event_risk is None else event_risk.state.value
    state_key = "|".join(
        (
            policy.feature_version,
            underlying.snapshot_id,
            as_of.astimezone(UTC).isoformat(),
            trend.value,
            volatility.value,
        )
    )
    return MarketState(
        market_state_id=hashlib.sha256(state_key.encode()).hexdigest()[:24],
        feature_version=policy.feature_version,
        calculated_at=as_of,
        source_snapshot_ids=source_ids,
        trend=trend,
        volatility=volatility,
        return_15m=values.get("return_15m"),
        return_60m=values.get("return_60m"),
        normalized_return_15m=values.get("normalized_15m"),
        normalized_return_60m=values.get("normalized_60m"),
        normalized_vwap_distance=values.get("vwap_distance"),
        realized_volatility_ratio=values.get("rv_ratio"),
        realized_volatility_annualized=rv,
        iv_percentile=iv_percentile,
        iv_rv_ratio=iv_rv,
        trend_score=trend_score,
        close_relative_move=_close_relative(underlying),
        event_state=event_state,
        macro_status=macro_status,
        quality=quality,
        warmup_complete=warm,
        completed_bar_count=len(bars),
        session_count=len(sessions),
        reason_codes=tuple(dict.fromkeys(reasons)),
    )


def _completed_bars(event: CanonicalMarketEvent | None, as_of: datetime) -> list[_Bar]:
    if (
        event is None
        or event.event_type != "BAR_SNAPSHOT"
        or event.payload.get("resolution") != "5"
    ):
        return []
    rows = event.payload.get("bars", [])
    if not isinstance(rows, list):
        return []
    result: list[_Bar] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            stamp = datetime.fromtimestamp(int(row["timestamp"]), tz=UTC)
            open_price = Decimal(str(row["open"]))
            high = Decimal(str(row["high"]))
            low = Decimal(str(row["low"]))
            close = Decimal(str(row["close"]))
            volume = Decimal(str(row["volume"]))
        except (KeyError, ArithmeticError, TypeError, ValueError, OSError):
            continue
        if stamp + timedelta(seconds=_BAR_SECONDS) > as_of:
            continue
        if min(open_price, high, low, close) <= 0 or volume < 0:
            continue
        result.append((stamp, open_price, high, low, close, volume))
    result.sort(key=lambda item: item[0])
    return result


def _features(bars: Sequence[_Bar]) -> dict[str, Decimal]:
    if len(bars) < _ATR_PERIOD:
        return {}
    closes = [row[4] for row in bars]
    trs: list[Decimal] = []
    for index in range(1, len(bars)):
        high, low, previous = bars[index][2], bars[index][3], closes[index - 1]
        trs.append(max(high - low, abs(high - previous), abs(low - previous)))
    atr = sum(trs[-_ATR_PERIOD:], _ZERO) / Decimal(min(len(trs), _ATR_PERIOD))
    last = closes[-1]
    if atr <= 0 or last <= 0 or len(closes) < _RETURN_60_BARS:
        return {}
    r15 = last / closes[-4] - 1
    r60 = last / closes[-13] - 1
    atr_pct = atr / last
    n15 = _clamp(r15 / (atr_pct * Decimal(3).sqrt()), Decimal(-2), Decimal(2)) / 2
    n60 = _clamp(r60 / (atr_pct * Decimal(12).sqrt()), Decimal(-2), Decimal(2)) / 2
    volume_total = sum((row[5] for row in bars), _ZERO)
    vwap = (
        last
        if volume_total <= 0
        else sum(
            (((row[2] + row[3] + row[4]) / 3) * row[5] for row in bars),
            _ZERO,
        )
        / volume_total
    )
    vwap_distance = _clamp((last - vwap) / atr, Decimal(-1), Decimal(1))
    returns = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]
    short_rv = _stdev(returns[-12:])
    long_rv = _stdev(returns[-min(len(returns), 50) :])
    rv_ratio = None if long_rv <= 0 else short_rv / long_rv
    annualized = long_rv * _ANNUAL_BARS.sqrt() * 100
    result = {
        "return_15m": _q(r15),
        "return_60m": _q(r60),
        "normalized_15m": _q(n15),
        "normalized_60m": _q(n60),
        "vwap_distance": _q(vwap_distance),
        "annualized_rv": _q(annualized),
    }
    if rv_ratio is not None:
        result["rv_ratio"] = _q(rv_ratio)
    return result


def _trend_score(values: dict[str, Decimal]) -> Decimal | None:
    required = ("normalized_15m", "normalized_60m", "vwap_distance")
    if any(key not in values for key in required):
        return None
    score = (
        Decimal("0.35") * values[required[0]]
        + Decimal("0.45") * values[required[1]]
        + Decimal("0.20") * values[required[2]]
    )
    return _q(_clamp(score, -_ONE, _ONE))


def _trend(
    values: dict[str, Decimal],
    score: Decimal | None,
    warm: bool,
    policy: IdentificationPolicy,
) -> TrendState:
    if not warm or score is None:
        return TrendState.UNKNOWN
    r15, r60 = values.get("return_15m"), values.get("return_60m")
    if r15 is None or r60 is None:
        return TrendState.UNKNOWN
    if r15 > 0 and r60 > 0 and score >= policy.regime.trend_threshold:
        return TrendState.UP
    if r15 < 0 and r60 < 0 and score <= -policy.regime.trend_threshold:
        return TrendState.DOWN
    if abs(score) <= policy.regime.range_threshold:
        return TrendState.RANGE
    return TrendState.MIXED


def _volatility(
    ratio: Decimal | None, warm: bool, policy: IdentificationPolicy
) -> VolatilityState:
    if not warm or ratio is None:
        return VolatilityState.UNKNOWN
    if ratio >= policy.regime.volatility_expansion_ratio:
        return VolatilityState.EXPANDING
    if ratio <= policy.regime.volatility_compression_ratio:
        return VolatilityState.COMPRESSED
    return VolatilityState.NORMAL


def _current_iv(candidates: Sequence[FeatureSnapshot]) -> Decimal | None:
    values = [
        item.derivatives.greeks.implied_volatility
        for item in candidates
        if item.derivatives is not None
        and item.derivatives.greeks is not None
        and item.derivatives.greeks.converged
        and item.derivatives.greeks.implied_volatility is not None
    ]
    return None if not values else Decimal(str(median(values)))


def _percentile(value: Decimal | None, history: Sequence[Decimal]) -> Decimal | None:
    if value is None or not history:
        return None
    valid = [item for item in history if item >= 0]
    if not valid:
        return None
    below = sum(1 for item in valid if item <= value)
    return _q(Decimal(below) / Decimal(len(valid)) * 100)


def _macro_status(
    macro: MacroAssessment | None, trend: TrendState, now: datetime
) -> MacroStatus:
    if macro is None:
        return MacroStatus.MISSING
    if now >= macro.fresh_until:
        return MacroStatus.STALE
    bias = accepted_macro_bias(macro, now=now, min_confidence=Decimal("0.6"))
    if bias is None:
        return MacroStatus.NEUTRAL
    expected = {
        TrendState.UP: MacroBias.BULLISH,
        TrendState.DOWN: MacroBias.BEARISH,
    }.get(trend, MacroBias.NEUTRAL)
    if expected is MacroBias.NEUTRAL:
        return MacroStatus.NEUTRAL
    return MacroStatus.ALIGNED if bias is expected else MacroStatus.CONFLICT


def _close_relative(snapshot: FeatureSnapshot) -> Decimal | None:
    last, close = snapshot.market.last, snapshot.market.close
    if last is None or close is None or close.value <= 0:
        return None
    return _q((last.value - close.value) / close.value)


def _stdev(values: Sequence[Decimal]) -> Decimal:
    if len(values) < _MIN_STDEV_SAMPLES:
        return _ZERO
    mean = sum(values, _ZERO) / Decimal(len(values))
    variance = sum(((item - mean) ** 2 for item in values), _ZERO) / Decimal(
        len(values) - 1
    )
    return variance.sqrt()


def _clamp(value: Decimal, low: Decimal, high: Decimal) -> Decimal:
    return min(max(value, low), high)


def _q(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.000001"))
