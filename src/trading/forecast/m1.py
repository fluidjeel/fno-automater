"""M1 microstructure forecaster: direction from order flow, minutes horizon."""

from __future__ import annotations

from datetime import time
from decimal import Decimal

from trading.config.forecast import ForecastConfig
from trading.domain.enums import ModeId, ReasonCode
from trading.forecast.common import assemble_view, horizon_sigma, score_model
from trading.forecast.inputs import ForecastInputs, ModeView, annual_vol, clamp

__all__ = ["M1_TIME_BUCKETS", "m1_cost_hurdle", "m1_time_bucket", "m1_view"]

_ZERO = Decimal(0)
_ONE = Decimal(1)
_TWO_OVER_PI_SQRT = Decimal("0.7978845608028654")
_BUCKETS: tuple[tuple[str, time, time], ...] = (
    ("open", time(9, 15), time(9, 45)),
    ("morning", time(9, 45), time(11, 30)),
    ("midday", time(11, 30), time(13, 30)),
    ("afternoon", time(13, 30), time(14, 30)),
    ("close", time(14, 30), time(15, 30)),
)
M1_TIME_BUCKETS = tuple(name for name, _start, _end in _BUCKETS)


def m1_time_bucket(local: time) -> str | None:
    """Session segment; microstructure behaves differently in each."""
    for name, start, end in _BUCKETS:
        if start <= local < end:
            return name
    return None


def m1_view(inputs: ForecastInputs, config: ForecastConfig) -> ModeView:
    """Order-flow driven directional view; the 5m trend is context only."""
    mode = config.m1
    features: dict[str, Decimal] = {}
    absent: list[str] = []
    reasons: list[ReasonCode] = []
    flow = inputs.order_flow
    if flow is not None:
        if flow.ofi_short is not None:
            features["ofi_short"] = clamp(flow.ofi_short, Decimal(-5), Decimal(5))
        if flow.ofi_long is not None:
            features["ofi_long"] = clamp(flow.ofi_long, Decimal(-5), Decimal(5))
        if flow.microprice_drift_bps is not None:
            features["microprice_drift_bps"] = clamp(
                flow.microprice_drift_bps, Decimal(-50), Decimal(50)
            )
    auction = inputs.underlying_features.get("cas_auction_imbalance")
    trade = inputs.underlying_features.get("cas_trade_flow_imbalance")
    if auction is not None and trade is not None:
        features["cas_pressure"] = clamp(auction + trade, Decimal(-2), Decimal(2))
    lead = inputs.underlying_features.get("futures_lead_bps")
    if lead is not None:
        features["futures_lead_bps"] = clamp(lead, Decimal(-50), Decimal(50))
    if inputs.market is not None and inputs.market.trend_score is not None:
        features["trend_context"] = inputs.market.trend_score
    local = inputs.as_of.astimezone(inputs.zone).time()
    bucket = m1_time_bucket(local)
    for name in M1_TIME_BUCKETS:
        features[f"bucket_{name}"] = _ONE if name == bucket else _ZERO
    features["expiry_day"] = _ONE if inputs.expiry_today else _ZERO
    if bucket is None or bucket in mode.blocked_time_buckets:
        reasons.append(ReasonCode.FORECAST_OUTSIDE_ENTRY_WINDOW)
    if not any(key in features for key in ("ofi_short", "ofi_long", "cas_pressure")):
        reasons.append(ReasonCode.FORECAST_INPUT_ABSENT)

    p_up, missing = score_model(mode, features)
    absent.extend(missing)
    implied = annual_vol(inputs.current_vix)
    sigma, vol_absent = horizon_sigma(
        config,
        mode,
        bars=inputs.bars,
        daily_variance=None,
        implied_vol=implied,
        intraday_weight=_ONE,
    )
    absent.extend(item for item in vol_absent if item != "daily_realized_vol")
    if sigma is None:
        sigma = Decimal("0.001")
        reasons.append(ReasonCode.FORECAST_INPUT_ABSENT)
    return assemble_view(
        mode_id=ModeId.M1_CAS,
        config=config,
        mode=mode,
        p_up=p_up,
        sigma=sigma,
        implied_vol=implied,
        features=features,
        absent=absent,
        reasons=reasons,
    )


def m1_cost_hurdle(
    *,
    spot: Decimal,
    sigma: Decimal,
    delta: Decimal | None,
    gamma: Decimal | None,
    spread: Decimal,
    cost_per_unit: Decimal,
    multiple: Decimal,
) -> tuple[bool, Decimal]:
    """Expected premium move must beat ``multiple`` round trips of friction.

    Expected move of the option is delta times E|dS| plus the gamma term on
    E[dS^2]; friction is the full spread plus per-unit charges.
    """
    move = spot * sigma
    expected = abs(delta or _ZERO) * move * _TWO_OVER_PI_SQRT
    if gamma is not None:
        expected += gamma * move * move / 2
    friction = spread + cost_per_unit
    return expected >= multiple * friction, expected
