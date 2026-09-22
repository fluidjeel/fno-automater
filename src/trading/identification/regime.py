"""Market regime classifier.

Builds on the existing MarketState and adds a broader market regime classification.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import Field

from trading.domain.contracts.base import ExactDecimal, StrictModel
from trading.domain.contracts.identification import TrendState
from trading.universe.contracts import MarketRegime, TradeDirection

__all__ = ["RegimeConfig", "classify_regime", "regime_size_multiplier"]


class RegimeConfig(StrictModel):
    # Advance-decline ratio thresholds
    strong_bull_breadth_min: ExactDecimal = Field(default=Decimal("60"))
    mild_bull_breadth_min: ExactDecimal = Field(default=Decimal("40"))
    strong_bear_breadth_max: ExactDecimal = Field(default=Decimal("40"))
    # VIX thresholds
    low_vix_threshold: ExactDecimal = Field(default=Decimal("15"))
    high_vix_threshold: ExactDecimal = Field(default=Decimal("20"))
    # Regime sizing multipliers
    strong_bull_long_multiplier: ExactDecimal = Field(default=Decimal("1.5"))
    strong_bull_short_multiplier: ExactDecimal = Field(default=Decimal("0.3"))
    mild_bull_long_multiplier: ExactDecimal = Field(default=Decimal("1.2"))
    mild_bull_short_multiplier: ExactDecimal = Field(default=Decimal("0.5"))
    neutral_multiplier: ExactDecimal = Field(default=Decimal("0.8"))
    mild_bear_long_multiplier: ExactDecimal = Field(default=Decimal("0.5"))
    mild_bear_short_multiplier: ExactDecimal = Field(default=Decimal("1.2"))
    strong_bear_long_multiplier: ExactDecimal = Field(default=Decimal("0.3"))
    strong_bear_short_multiplier: ExactDecimal = Field(default=Decimal("1.5"))


def classify_regime(
    *,
    nifty_trend: TrendState,
    breadth_pct: Decimal,
    current_vix: Decimal | None,
    iv_percentile: Decimal | None,
    config: RegimeConfig,
) -> MarketRegime:
    if nifty_trend is TrendState.UP:
        if (
            breadth_pct > config.strong_bull_breadth_min
            and current_vix is not None
            and current_vix < config.low_vix_threshold
        ):
            return MarketRegime.STRONG_BULL
        if (
            config.mild_bull_breadth_min
            <= breadth_pct
            <= config.strong_bull_breadth_min
        ):
            return MarketRegime.MILD_BULL

    elif nifty_trend is TrendState.DOWN:
        if (
            breadth_pct < config.strong_bear_breadth_max
            and current_vix is not None
            and current_vix > config.high_vix_threshold
        ):
            return MarketRegime.STRONG_BEAR
        if (
            config.strong_bear_breadth_max
            <= breadth_pct
            <= config.strong_bull_breadth_min
        ):
            return MarketRegime.MILD_BEAR

    return MarketRegime.NEUTRAL


def regime_size_multiplier(
    regime: MarketRegime,
    direction: TradeDirection,
    config: RegimeConfig,
) -> Decimal:
    if regime is MarketRegime.STRONG_BULL:
        return (
            config.strong_bull_long_multiplier
            if direction is TradeDirection.LONG
            else config.strong_bull_short_multiplier
        )
    if regime is MarketRegime.MILD_BULL:
        return (
            config.mild_bull_long_multiplier
            if direction is TradeDirection.LONG
            else config.mild_bull_short_multiplier
        )
    if regime is MarketRegime.STRONG_BEAR:
        return (
            config.strong_bear_long_multiplier
            if direction is TradeDirection.LONG
            else config.strong_bear_short_multiplier
        )
    if regime is MarketRegime.MILD_BEAR:
        return (
            config.mild_bear_long_multiplier
            if direction is TradeDirection.LONG
            else config.mild_bear_short_multiplier
        )

    return config.neutral_multiplier
