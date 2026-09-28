"""Triple-barrier horizon labels for mode forecasts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import count
from typing import Any

from trading.analytics.forecast_labels import (
    LabelTiming,
    bars_from_payloads,
    label_forecast,
    label_forecasts,
    split_forecast_records,
)
from trading.domain.contracts.forecast import (
    ForecastBarrier,
    ForecastEventKind,
    ForecastLeg,
    ModeForecast,
)
from trading.domain.enums import ModeId, OptionType, Side
from trading.forecast.bars import Bar

NOW = datetime(2026, 9, 14, 4, 0, tzinfo=UTC)
TIMING = LabelTiming(bar_seconds=300, session_minutes=375, annual_trading_days=252)


def _forecast(**overrides: Any) -> ModeForecast:
    return ModeForecast.model_validate(
        {
            "forecast_id": "FC-1",
            "cycle_id": "FCY-1",
            "as_of": NOW,
            "mode_id": ModeId.M2_DIRECTIONAL,
            "model_version": "forecast-v1",
            "config_version": "forecast-v1+abc",
            "horizon_seconds": 1_800,
            "spot": Decimal("100"),
            "direction": 1,
            "p_up": Decimal("0.6"),
            "event": ForecastEventKind.UP_MOVE,
            "event_threshold": Decimal("0.005"),
            "p_event": Decimal("0.3"),
            "forecast_drift": Decimal("0.001"),
            "forecast_sigma": Decimal("0.01"),
            "target_move_fraction": Decimal("0.01"),
            "stop_move_fraction": Decimal("0.005"),
            "gate_passed": True,
            "gate_enforced": False,
            **overrides,
        }
    )


def _bars(*rows: tuple[str, str, str], start: datetime = NOW) -> tuple[Bar, ...]:
    """Build bars from (high, low, close) with open at the previous close."""
    bars: list[Bar] = []
    previous = Decimal("100")
    for index, (high, low, close) in enumerate(rows):
        bars.append(
            Bar(
                start=start + timedelta(minutes=5 * index),
                open=previous,
                high=Decimal(high),
                low=Decimal(low),
                close=Decimal(close),
                volume=Decimal(1),
            )
        )
        previous = Decimal(close)
    return tuple(bars)


FLAT = ("100.2", "99.8", "100")


class TestTripleBarrier:
    def test_target_before_stop(self) -> None:
        """An up-view that runs to +1% first is labelled TARGET."""
        bars = _bars(("100.4", "99.9", "100.3"), ("101.1", "100.2", "101"), *[FLAT] * 4)
        label = label_forecast(
            _forecast(), bars, timing=TIMING, labelled_at=NOW, label_id="L"
        )
        assert label is not None
        assert label.barrier is ForecastBarrier.TARGET
        assert label.barrier_at == bars[1].start
        assert label.max_favorable >= Decimal("0.011")

    def test_same_bar_touch_counts_as_stop(self) -> None:
        """Ambiguous intrabar order is resolved against the forecast."""
        bars = _bars(("101.5", "99.4", "100"), *[FLAT] * 5)
        label = label_forecast(
            _forecast(), bars, timing=TIMING, labelled_at=NOW, label_id="L"
        )
        assert label is not None
        assert label.barrier is ForecastBarrier.STOP

    def test_down_view_measures_excursions_in_thesis_direction(self) -> None:
        """For a bearish view a fall is favourable and positive."""
        forecast = _forecast(direction=-1, event=ForecastEventKind.DOWN_MOVE)
        bars = _bars(("100.1", "98.9", "99"), *[("99.2", "98.8", "99")] * 5)
        label = label_forecast(
            forecast, bars, timing=TIMING, labelled_at=NOW, label_id="L"
        )
        assert label is not None
        assert label.barrier is ForecastBarrier.TARGET
        assert label.max_favorable > 0
        assert label.event_occurred

    def test_band_view_stops_when_band_breaks(self) -> None:
        """A short-vol STAY_IN_BAND thesis fails on the first band touch."""
        forecast = _forecast(direction=0, event=ForecastEventKind.STAY_IN_BAND)
        bars = _bars(*[FLAT] * 3, ("100.7", "100", "100.6"), *[FLAT] * 2)
        label = label_forecast(
            forecast, bars, timing=TIMING, labelled_at=NOW, label_id="L"
        )
        assert label is not None
        assert label.barrier is ForecastBarrier.STOP
        assert label.event_occurred  # terminal close is back inside the band


class TestHorizonAccounting:
    def test_not_labelled_before_horizon_completes(self) -> None:
        """No look-ahead gaps: an unfinished horizon produces no label."""
        label = label_forecast(
            _forecast(),
            _bars(*[FLAT] * 3),
            timing=TIMING,
            labelled_at=NOW,
            label_id="L",
        )
        assert label is None

    def test_stale_incomplete_path_is_labelled_incomplete(self) -> None:
        """Data gaps surface as complete=False instead of vanishing."""
        label = label_forecast(
            _forecast(),
            _bars(*[FLAT] * 3),
            timing=TIMING,
            labelled_at=NOW + timedelta(days=10),
            label_id="L",
        )
        assert label is not None
        assert not label.complete
        assert label.bars_used == 3

    def test_bars_before_decision_are_excluded(self) -> None:
        """Only bars starting at or after the decision instant are used."""
        before = _bars(("110", "90", "100"), start=NOW - timedelta(minutes=5))
        label = label_forecast(
            _forecast(),
            (*before[:1], *_bars(*[FLAT] * 6)),
            timing=TIMING,
            labelled_at=NOW,
            label_id="L",
        )
        assert label is not None
        assert label.barrier is ForecastBarrier.TIMEOUT
        assert label.realized_return == Decimal(0)


class TestStructureRealization:
    def test_realized_edge_subtracts_cost_and_friction(self) -> None:
        """Realized edge uses the same cost basis as the forecast edge."""
        leg = ForecastLeg(
            symbol="C100",
            option_type=OptionType.CALL,
            strike=Decimal("100"),
            side=Side.BUY,
            ratio=1,
            expiry_years=Decimal("0"),
            mid=Decimal("1"),
        )
        forecast = _forecast(
            legs=(leg,),
            structure_cost=Decimal("1"),
            cost_per_unit=Decimal("0.1"),
            forecast_value=Decimal("1.5"),
            edge_after_costs=Decimal("0.4"),
        )
        bars = _bars(*[FLAT] * 5, ("102.2", "101.8", "102"))
        label = label_forecast(
            forecast, bars, timing=TIMING, labelled_at=NOW, label_id="L"
        )
        assert label is not None
        assert label.realized_structure_value == Decimal("2.00000000")
        assert label.realized_edge == Decimal("0.90000000")


class TestBatch:
    def test_skips_already_labelled(self) -> None:
        """Relabelling is idempotent by forecast id."""
        ids = count()
        labels = label_forecasts(
            [_forecast(), _forecast(forecast_id="FC-2")],
            _bars(*[FLAT] * 6),
            timing=TIMING,
            labelled_at=NOW,
            new_id=lambda prefix: f"{prefix}-{next(ids)}",
            already_labelled=frozenset({"FC-1"}),
        )
        assert [label.forecast_id for label in labels] == ["FC-2"]

    def test_payload_bars_are_merged_by_start(self) -> None:
        """Overlapping BAR_SNAPSHOT windows produce one bar per start."""
        stamp = int(NOW.timestamp())
        row = {"timestamp": stamp, "open": 1, "high": 2, "low": 1, "close": 2}
        later = {**row, "close": 1.5}
        bars = bars_from_payloads(
            [{"bars": [row]}, {"bars": [later]}],
            bar_seconds=300,
            as_of=NOW + timedelta(hours=1),
        )
        assert len(bars) == 1
        assert bars[0].close == Decimal("1.5")

    def test_split_ignores_unrelated_payloads(self) -> None:
        forecasts, labels = split_forecast_records([_forecast(), object()])
        assert len(forecasts) == 1
        assert labels == ()
