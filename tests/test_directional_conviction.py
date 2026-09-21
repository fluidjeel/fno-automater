"""Tests for the FNO universe scanner, regime classifier, and conviction system."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trading.domain.contracts.identification import TrendState
from trading.identification.regime import (
    RegimeConfig,
    classify_regime,
    regime_size_multiplier,
)
from trading.trade.eod_scanner import compute_positional_stop, evaluate_carry_forward
from trading.universe.contracts import (
    CarryForwardAction,
    ConvictionAssessment,
    ConvictionLevel,
    MarketRegime,
    StockScore,
    TradeDirection,
    UniverseScanResult,
)
from trading.universe.ranking import compute_stock_score, rank_universe
from trading.universe.sector import NIFTY_SECTORS, classify_sector


# ---- Fixtures ---------------------------------------------------------------

_NOW = datetime(2026, 9, 20, 10, 0, 0, tzinfo=UTC)
_CONFIG = RegimeConfig()


def _make_bars(
    count: int = 60,
    start_close: Decimal = Decimal("100"),
    trend: Decimal = Decimal("0.001"),
) -> list[tuple[datetime, Decimal, Decimal, Decimal, Decimal, Decimal]]:
    """Generate synthetic OHLCV bars with a slight trend."""
    bars = []
    for i in range(count):
        close = start_close * (1 + trend * i)
        open_p = close * Decimal("0.998")
        high = close * Decimal("1.003")
        low = close * Decimal("0.997")
        volume = Decimal("1000000")
        ts = _NOW - timedelta(minutes=(count - i) * 5)
        bars.append((ts, open_p, high, low, close, volume))
    return bars


def _make_conviction(
    score: Decimal = Decimal("80"),
    direction: TradeDirection = TradeDirection.LONG,
    level: ConvictionLevel = ConvictionLevel.STRONG,
) -> ConvictionAssessment:
    return ConvictionAssessment(
        assessment_id="test-conviction-001",
        symbol="RELIANCE",
        direction=direction,
        conviction_level=level,
        conviction_score=score,
        trend_alignment_score=Decimal("80"),
        volume_confirmation_score=Decimal("75"),
        oi_confirmation_score=Decimal("70"),
        price_action_score=Decimal("85"),
        sector_support_score=Decimal("60"),
        regime_alignment_score=Decimal("90"),
        assessed_at=_NOW,
    )


# ---- Universe Contracts Tests ------------------------------------------------


class TestMarketRegime:
    def test_enum_values(self) -> None:
        assert MarketRegime.STRONG_BULL == "STRONG_BULL"
        assert MarketRegime.STRONG_BEAR == "STRONG_BEAR"
        assert len(MarketRegime) == 5

    def test_trade_direction_values(self) -> None:
        assert TradeDirection.LONG == "LONG"
        assert TradeDirection.SHORT == "SHORT"

    def test_conviction_level_ordering(self) -> None:
        levels = list(ConvictionLevel)
        assert levels == [
            ConvictionLevel.NONE,
            ConvictionLevel.WEAK,
            ConvictionLevel.MODERATE,
            ConvictionLevel.STRONG,
            ConvictionLevel.VERY_STRONG,
        ]


class TestStockScore:
    def test_valid_construction(self) -> None:
        score = StockScore(
            symbol="RELIANCE",
            underlying="RELIANCE",
            direction=TradeDirection.LONG,
            composite_score=Decimal("75"),
            relative_strength=Decimal("0.05"),
            sector_rs=Decimal("0.02"),
            volume_surge_ratio=Decimal("1.8"),
            vwap_distance=Decimal("0.3"),
            oi_buildup_score=Decimal("1"),
            price_structure_score=Decimal("66"),
            sector="Energy",
            calculated_at=_NOW,
        )
        assert score.composite_score == Decimal("75")
        assert score.direction == TradeDirection.LONG

    def test_score_out_of_range_rejected(self) -> None:
        with pytest.raises(Exception):
            StockScore(
                symbol="RELIANCE",
                underlying="RELIANCE",
                direction=TradeDirection.LONG,
                composite_score=Decimal("150"),  # > 100
                relative_strength=Decimal("0"),
                sector_rs=None,
                volume_surge_ratio=Decimal("0"),
                vwap_distance=Decimal("0"),
                oi_buildup_score=Decimal("0"),
                price_structure_score=Decimal("0"),
                sector="Energy",
                calculated_at=_NOW,
            )


class TestConvictionAssessment:
    def test_round_trip(self) -> None:
        conv = _make_conviction()
        restored = ConvictionAssessment.model_validate(conv.model_dump(mode="json"))
        assert restored == conv

    def test_score_ranges(self) -> None:
        with pytest.raises(Exception):
            _make_conviction(score=Decimal("-10"))


# ---- Regime Classifier Tests -----------------------------------------------


class TestClassifyRegime:
    def test_strong_bull(self) -> None:
        result = classify_regime(
            nifty_trend=TrendState.UP,
            breadth_pct=Decimal("65"),
            current_vix=Decimal("12"),
            iv_percentile=Decimal("30"),
            config=_CONFIG,
        )
        assert result == MarketRegime.STRONG_BULL

    def test_mild_bull(self) -> None:
        result = classify_regime(
            nifty_trend=TrendState.UP,
            breadth_pct=Decimal("50"),
            current_vix=Decimal("18"),
            iv_percentile=Decimal("50"),
            config=_CONFIG,
        )
        assert result == MarketRegime.MILD_BULL

    def test_strong_bear(self) -> None:
        result = classify_regime(
            nifty_trend=TrendState.DOWN,
            breadth_pct=Decimal("30"),
            current_vix=Decimal("25"),
            iv_percentile=Decimal("80"),
            config=_CONFIG,
        )
        assert result == MarketRegime.STRONG_BEAR

    def test_mild_bear(self) -> None:
        result = classify_regime(
            nifty_trend=TrendState.DOWN,
            breadth_pct=Decimal("45"),
            current_vix=Decimal("18"),
            iv_percentile=Decimal("60"),
            config=_CONFIG,
        )
        assert result == MarketRegime.MILD_BEAR

    def test_neutral_range(self) -> None:
        result = classify_regime(
            nifty_trend=TrendState.RANGE,
            breadth_pct=Decimal("50"),
            current_vix=Decimal("15"),
            iv_percentile=Decimal("50"),
            config=_CONFIG,
        )
        assert result == MarketRegime.NEUTRAL

    def test_neutral_mixed(self) -> None:
        result = classify_regime(
            nifty_trend=TrendState.MIXED,
            breadth_pct=Decimal("50"),
            current_vix=Decimal("15"),
            iv_percentile=Decimal("50"),
            config=_CONFIG,
        )
        assert result == MarketRegime.NEUTRAL

    def test_neutral_when_vix_missing(self) -> None:
        """Without VIX, STRONG thresholds can't fire → falls to MILD or NEUTRAL."""
        result = classify_regime(
            nifty_trend=TrendState.UP,
            breadth_pct=Decimal("70"),
            current_vix=None,
            iv_percentile=None,
            config=_CONFIG,
        )
        # breadth > 60 but VIX is None → can't confirm STRONG_BULL
        # breadth 70 > strong_bull_breadth_min 60, outside 40-60 for MILD
        assert result in {MarketRegime.NEUTRAL, MarketRegime.MILD_BULL}


class TestRegimeSizeMultiplier:
    def test_strong_bull_long(self) -> None:
        mult = regime_size_multiplier(
            MarketRegime.STRONG_BULL, TradeDirection.LONG, _CONFIG
        )
        assert mult == Decimal("1.5")

    def test_strong_bull_short(self) -> None:
        mult = regime_size_multiplier(
            MarketRegime.STRONG_BULL, TradeDirection.SHORT, _CONFIG
        )
        assert mult == Decimal("0.3")

    def test_neutral(self) -> None:
        mult = regime_size_multiplier(
            MarketRegime.NEUTRAL, TradeDirection.LONG, _CONFIG
        )
        assert mult == Decimal("0.8")

    def test_strong_bear_short(self) -> None:
        mult = regime_size_multiplier(
            MarketRegime.STRONG_BEAR, TradeDirection.SHORT, _CONFIG
        )
        assert mult == Decimal("1.5")

    def test_multiplier_symmetry(self) -> None:
        """Bull long multiplier should equal bear short multiplier."""
        bull_long = regime_size_multiplier(
            MarketRegime.STRONG_BULL, TradeDirection.LONG, _CONFIG
        )
        bear_short = regime_size_multiplier(
            MarketRegime.STRONG_BEAR, TradeDirection.SHORT, _CONFIG
        )
        assert bull_long == bear_short


# ---- Ranking Tests ----------------------------------------------------------


class TestComputeStockScore:
    def test_uptrend_scores_long(self) -> None:
        bars = _make_bars(count=60, trend=Decimal("0.002"))
        nifty_bars = _make_bars(count=60, trend=Decimal("0.001"))
        score = compute_stock_score(
            symbol="RELIANCE",
            bars=bars,
            nifty_bars=nifty_bars,
            sector_bars=[],
            oi_data=[],
            as_of=_NOW,
        )
        assert score.symbol == "RELIANCE"
        assert score.direction == TradeDirection.LONG
        assert score.composite_score > Decimal("0")

    def test_downtrend_scores_short(self) -> None:
        bars = _make_bars(count=60, trend=Decimal("-0.003"))
        nifty_bars = _make_bars(count=60, trend=Decimal("0.001"))
        score = compute_stock_score(
            symbol="TATAMOTORS",
            bars=bars,
            nifty_bars=nifty_bars,
            sector_bars=[],
            oi_data=[],
            as_of=_NOW,
        )
        assert score.direction == TradeDirection.SHORT

    def test_insufficient_bars(self) -> None:
        """With < 60 bars, relative strength defaults to 0."""
        bars = _make_bars(count=25, trend=Decimal("0.001"))
        nifty_bars = _make_bars(count=25, trend=Decimal("0.001"))
        score = compute_stock_score(
            symbol="TCS",
            bars=bars,
            nifty_bars=nifty_bars,
            sector_bars=[],
            oi_data=[],
            as_of=_NOW,
        )
        assert score.relative_strength == Decimal("0")


class TestRankUniverse:
    def test_top_n_longs_and_shorts(self) -> None:
        scores = [
            StockScore(
                symbol=f"STOCK_{i}",
                underlying=f"STOCK_{i}",
                direction=TradeDirection.LONG if i % 2 == 0 else TradeDirection.SHORT,
                composite_score=Decimal(str(50 + i * 5)),
                relative_strength=Decimal("0"),
                sector_rs=None,
                volume_surge_ratio=Decimal("1"),
                vwap_distance=Decimal("0"),
                oi_buildup_score=Decimal("0"),
                price_structure_score=Decimal("50"),
                sector="Others",
                calculated_at=_NOW,
            )
            for i in range(10)
        ]
        longs, shorts = rank_universe(scores, MarketRegime.NEUTRAL, top_n=3)
        assert len(longs) <= 3
        assert len(shorts) <= 3

    def test_strong_bull_suppresses_shorts(self) -> None:
        scores = [
            StockScore(
                symbol=f"STOCK_{i}",
                underlying=f"STOCK_{i}",
                direction=TradeDirection.SHORT,
                composite_score=Decimal("30"),
                relative_strength=Decimal("-0.05"),
                sector_rs=None,
                volume_surge_ratio=Decimal("1"),
                vwap_distance=Decimal("-0.5"),
                oi_buildup_score=Decimal("-1"),
                price_structure_score=Decimal("20"),
                sector="Others",
                calculated_at=_NOW,
            )
            for i in range(5)
        ]
        longs, shorts = rank_universe(scores, MarketRegime.STRONG_BULL, top_n=5)
        assert len(shorts) == 0

    def test_strong_bear_suppresses_longs(self) -> None:
        scores = [
            StockScore(
                symbol=f"STOCK_{i}",
                underlying=f"STOCK_{i}",
                direction=TradeDirection.LONG,
                composite_score=Decimal("70"),
                relative_strength=Decimal("0.05"),
                sector_rs=None,
                volume_surge_ratio=Decimal("2"),
                vwap_distance=Decimal("0.5"),
                oi_buildup_score=Decimal("1"),
                price_structure_score=Decimal("80"),
                sector="Others",
                calculated_at=_NOW,
            )
            for i in range(5)
        ]
        longs, shorts = rank_universe(scores, MarketRegime.STRONG_BEAR, top_n=5)
        assert len(longs) == 0


# ---- Sector Tests -----------------------------------------------------------


class TestSector:
    def test_known_stock(self) -> None:
        assert classify_sector("RELIANCE") == "Energy"
        assert classify_sector("TCS") == "IT"
        assert classify_sector("HDFCBANK") == "Banking"

    def test_unknown_stock(self) -> None:
        assert classify_sector("UNKNOWN_STOCK") == "Others"

    def test_nifty_sectors_non_empty(self) -> None:
        assert len(NIFTY_SECTORS) >= 30


# ---- EOD Scanner Tests ------------------------------------------------------


class TestEvaluateCarryForward:
    def test_carry_forward_all_conditions_met(self) -> None:
        result = evaluate_carry_forward(
            symbol="RELIANCE",
            current_pnl=Decimal("5000"),
            conviction_at_close=_make_conviction(score=Decimal("80")),
            regime_aligned=True,
            min_conviction_for_carry=Decimal("65"),
            as_of=_NOW,
            decision_id="decision-001",
        )
        assert result.action == CarryForwardAction.CARRY_FORWARD
        assert result.in_profit is True

    def test_close_when_not_in_profit(self) -> None:
        result = evaluate_carry_forward(
            symbol="RELIANCE",
            current_pnl=Decimal("-2000"),
            conviction_at_close=_make_conviction(score=Decimal("80")),
            regime_aligned=True,
            min_conviction_for_carry=Decimal("65"),
            as_of=_NOW,
            decision_id="decision-002",
        )
        assert result.action == CarryForwardAction.CLOSE
        assert "not in profit" in result.reason

    def test_close_when_conviction_low(self) -> None:
        result = evaluate_carry_forward(
            symbol="RELIANCE",
            current_pnl=Decimal("5000"),
            conviction_at_close=_make_conviction(score=Decimal("50")),
            regime_aligned=True,
            min_conviction_for_carry=Decimal("65"),
            as_of=_NOW,
            decision_id="decision-003",
        )
        assert result.action == CarryForwardAction.CLOSE
        assert "conviction" in result.reason

    def test_close_when_regime_not_aligned(self) -> None:
        result = evaluate_carry_forward(
            symbol="RELIANCE",
            current_pnl=Decimal("5000"),
            conviction_at_close=_make_conviction(score=Decimal("80")),
            regime_aligned=False,
            min_conviction_for_carry=Decimal("65"),
            as_of=_NOW,
            decision_id="decision-004",
        )
        assert result.action == CarryForwardAction.CLOSE
        assert "regime" in result.reason


class TestComputePositionalStop:
    def test_long_stop(self) -> None:
        stop = compute_positional_stop(
            entry_price=Decimal("500"),
            current_price=Decimal("520"),
            direction="LONG",
            atr=Decimal("15"),
            previous_day_high=Decimal("510"),
            previous_day_low=Decimal("485"),
        )
        # max(485, 500 - 30) = max(485, 470) = 485
        assert stop == Decimal("485")

    def test_short_stop(self) -> None:
        stop = compute_positional_stop(
            entry_price=Decimal("500"),
            current_price=Decimal("480"),
            direction="SHORT",
            atr=Decimal("15"),
            previous_day_high=Decimal("510"),
            previous_day_low=Decimal("475"),
        )
        # min(510, 500 + 30) = min(510, 530) = 510
        assert stop == Decimal("510")

    def test_invalid_direction_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown direction"):
            compute_positional_stop(
                entry_price=Decimal("500"),
                current_price=Decimal("520"),
                direction="INVALID",
                atr=Decimal("15"),
                previous_day_high=Decimal("510"),
                previous_day_low=Decimal("485"),
            )


# ---- Sizing Safety Property Tests -------------------------------------------


class TestSizingSafety:
    """Verify that regime multipliers never breach hard limits."""

    @pytest.mark.parametrize("regime", list(MarketRegime))
    @pytest.mark.parametrize("direction", list(TradeDirection))
    def test_multiplier_is_positive(
        self, regime: MarketRegime, direction: TradeDirection
    ) -> None:
        mult = regime_size_multiplier(regime, direction, _CONFIG)
        assert mult > Decimal("0")

    @pytest.mark.parametrize("regime", list(MarketRegime))
    @pytest.mark.parametrize("direction", list(TradeDirection))
    def test_multiplier_bounded(
        self, regime: MarketRegime, direction: TradeDirection
    ) -> None:
        mult = regime_size_multiplier(regime, direction, _CONFIG)
        # Multiplier should be between 0 and 2 (reasonable bounds)
        assert Decimal("0") < mult <= Decimal("2")
