"""Per-mode calibration scoring and the missed-move ledger."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from trading.analytics.forecast_calibration import (
    calibrate_modes,
    format_calibration_report,
)
from trading.analytics.missed_moves import (
    detect_large_moves,
    format_missed_moves,
    missed_moves,
)
from trading.domain.contracts.discovery_decision import (
    DiscoveryDecision,
    DiscoveryDecisionInputs,
)
from trading.domain.contracts.forecast import (
    ForecastBarrier,
    ForecastEventKind,
    ForecastLabel,
    ModeForecast,
)
from trading.domain.enums import (
    DiscoveryDecisionKind,
    DiscoveryStage,
    ModeId,
    ReasonCode,
)
from trading.forecast.bars import Bar

NOW = datetime(2026, 9, 14, 4, 0, tzinfo=UTC)


def _forecast(index: int, p_up: str, **overrides: Any) -> ModeForecast:
    return ModeForecast.model_validate(
        {
            "forecast_id": f"FC-{index}",
            "cycle_id": f"FCY-{index}",
            "as_of": NOW + timedelta(minutes=index),
            "mode_id": ModeId.M2_DIRECTIONAL,
            "model_version": "v1",
            "config_version": "v1+abc",
            "horizon_seconds": 1_800,
            "spot": Decimal("100"),
            "direction": 1 if Decimal(p_up) > Decimal("0.5") else -1,
            "p_up": Decimal(p_up),
            "event": ForecastEventKind.UP_MOVE,
            "event_threshold": Decimal("0.005"),
            "p_event": Decimal(p_up),
            "forecast_drift": Decimal("0"),
            "forecast_sigma": Decimal("0.01"),
            "target_move_fraction": Decimal("0.01"),
            "stop_move_fraction": Decimal("0.005"),
            "gate_passed": True,
            "gate_enforced": False,
            "features": {"x": Decimal(1) if Decimal(p_up) > Decimal("0.5") else -1},
            **overrides,
        }
    )


def _label(index: int, realized: str, *, complete: bool = True) -> ForecastLabel:
    up = Decimal(realized) > 0
    return ForecastLabel(
        label_id=f"L-{index}",
        forecast_id=f"FC-{index}",
        mode_id=ModeId.M2_DIRECTIONAL,
        labelled_at=NOW + timedelta(days=1),
        horizon_end=NOW + timedelta(hours=1),
        complete=complete,
        barrier=ForecastBarrier.TARGET if up else ForecastBarrier.STOP,
        event_occurred=up,
        realized_return=Decimal(realized),
        max_favorable=Decimal("0.01"),
        max_adverse=Decimal("-0.01"),
        bars_used=6,
    )


class TestCalibration:
    def test_informative_forecasts_beat_base_rate(self) -> None:
        """A model that ranks outcomes correctly has positive Brier skill."""
        forecasts = [_forecast(i, "0.8" if i % 2 else "0.2") for i in range(40)]
        labels = [_label(i, "0.01" if i % 2 else "-0.01") for i in range(40)]
        (result,) = calibrate_modes(forecasts, labels)
        assert result.direction is not None
        assert result.direction.skill_vs_base > 0
        assert result.conviction[-1].hit_rate == Decimal("1.0000")

    def test_uninformative_forecasts_have_no_skill(self) -> None:
        """Constant 0.5 never beats the realized base rate."""
        forecasts = [_forecast(i, "0.5") for i in range(20)]
        labels = [_label(i, "0.01" if i % 2 else "-0.01") for i in range(20)]
        (result,) = calibrate_modes(forecasts, labels)
        assert result.direction is not None
        assert result.direction.skill_vs_base <= 0

    def test_incomplete_labels_are_counted_not_scored(self) -> None:
        """Data-gap labels must not bias the score."""
        forecasts = [_forecast(0, "0.7"), _forecast(1, "0.7")]
        labels = [_label(0, "0.01"), _label(1, "-0.01", complete=False)]
        (result,) = calibrate_modes(forecasts, labels)
        assert result.labelled == 1
        assert result.incomplete == 1

    def test_implied_baseline_is_scored_when_present(self) -> None:
        """The market's own probability is the bar the mode must clear."""
        forecasts = [
            _forecast(i, "0.6", p_event_implied=Decimal("0.5")) for i in range(10)
        ]
        labels = [_label(i, "0.01") for i in range(10)]
        (result,) = calibrate_modes(forecasts, labels)
        assert result.event is not None
        assert result.event.brier_implied == Decimal("0.2500")
        assert result.event.implied_samples == 10

    def test_refit_uses_chronological_holdout(self) -> None:
        """Refit is a proposal scored on later data it never saw."""
        forecasts = [_forecast(i, "0.8" if i % 2 else "0.2") for i in range(50)]
        labels = [_label(i, "0.01" if i % 2 else "-0.01") for i in range(50)]
        (result,) = calibrate_modes(
            forecasts,
            labels,
            current_models={ModeId.M2_DIRECTIONAL: (Decimal(0), {"x": Decimal(0)})},
            min_refit_samples=20,
        )
        assert result.refit is not None
        assert result.refit.train_samples == 35
        assert result.refit.holdout_samples == 15
        assert result.refit.improves
        assert "PROPOSE" in format_calibration_report((result,))


def _bars(closes: list[str]) -> tuple[Bar, ...]:
    bars: list[Bar] = []
    previous = Decimal(closes[0])
    for index, close in enumerate(closes):
        value = Decimal(close)
        bars.append(
            Bar(
                start=NOW + timedelta(minutes=5 * index),
                open=previous,
                high=max(previous, value) + Decimal("0.1"),
                low=min(previous, value) - Decimal("0.1"),
                close=value,
                volume=Decimal(1),
            )
        )
        previous = value
    return tuple(bars)


def _decision(at: datetime, kind: DiscoveryDecisionKind) -> DiscoveryDecision:
    return DiscoveryDecision(
        decision_id=f"D-{at.isoformat()}",
        cycle_id="C",
        as_of=at,
        mode_id=ModeId.M2_DIRECTIONAL,
        family_id="long_call",
        strategy_id="long_option",
        experiment_id="EXP",
        decision=kind,
        stage=DiscoveryStage.STRATEGY,
        reason_codes=(ReasonCode.FORECAST_EDGE_INSUFFICIENT,),
        reason_text="edge below hurdle",
        profile_version="p",
        code_version="c",
        inputs=DiscoveryDecisionInputs(),
    )


class TestMissedMoves:
    def _moves(self) -> tuple[Any, ...]:
        quiet = ["100", "100.2", "100", "100.2"] * 6
        breakout = ["100.5", "101", "101.5", "102"]
        return detect_large_moves(
            _bars([*quiet, *breakout]),
            horizon_bars=4,
            bar_seconds=300,
            threshold_atr=Decimal("1.5"),
            atr_windows=3,
            session_bars=75,
        )

    def test_detects_only_the_breakout(self) -> None:
        """ATR yardstick uses only preceding windows (no look-ahead)."""
        moves = self._moves()
        assert len(moves) == 1
        assert moves[0].direction == 1
        assert moves[0].start == NOW + timedelta(minutes=5 * 24)

    def test_intraday_windows_do_not_span_overnight(self) -> None:
        """A 15-minute window never pairs a close with the next morning's open."""
        evening = _bars(["100", "100.1", "100", "100.1"])
        morning = tuple(
            Bar(
                start=bar.start + timedelta(hours=18),
                open=bar.open + 3,
                high=bar.high + 3,
                low=bar.low + 3,
                close=bar.close + 3,
                volume=bar.volume,
            )
            for bar in evening[:3]
        )
        moves = detect_large_moves(
            (*evening, *morning),
            horizon_bars=2,
            bar_seconds=300,
            threshold_atr=Decimal("0.5"),
            atr_windows=1,
            session_bars=75,
        )
        assert all(move.start.date() == move.end.date() for move in moves)
        assert all(abs(move.move_fraction) < Decimal("0.01") for move in moves)

    def test_blocked_move_names_the_gate(self) -> None:
        """A right view that did not trade is BLOCKED with its reason."""
        (move,) = self._moves()
        forecast = _forecast(0, "0.7", as_of=move.start + timedelta(minutes=1))
        rows = missed_moves(
            ModeId.M2_DIRECTIONAL,
            (move,),
            forecasts=[forecast],
            decisions=[_decision(move.start, DiscoveryDecisionKind.NO_TRADE)],
        )
        assert rows[0].verdict == "BLOCKED"
        assert rows[0].blocking_reasons[0][0] is ReasonCode.FORECAST_EDGE_INSUFFICIENT
        assert "BLOCKED" in format_missed_moves({ModeId.M2_DIRECTIONAL: rows})

    def test_wrong_view_and_caught(self) -> None:
        (move,) = self._moves()
        bearish = _forecast(0, "0.3", as_of=move.start)
        wrong = missed_moves(
            ModeId.M2_DIRECTIONAL, (move,), forecasts=[bearish], decisions=[]
        )
        assert wrong[0].verdict == "WRONG_VIEW"
        caught = missed_moves(
            ModeId.M2_DIRECTIONAL,
            (move,),
            forecasts=[],
            decisions=[_decision(move.start, DiscoveryDecisionKind.TRADE)],
        )
        assert caught[0].verdict == "CAUGHT"

    def test_other_modes_do_not_leak_in(self) -> None:
        (move,) = self._moves()
        rows = missed_moves(
            ModeId.M1_CAS,
            (move,),
            forecasts=[_forecast(0, "0.9", as_of=move.start)],
            decisions=[],
        )
        assert rows[0].verdict == "NOT_FORECAST"
