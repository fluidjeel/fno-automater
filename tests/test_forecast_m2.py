"""M2 directional forecaster and its forecast gate, edge and NIFTY-level exits."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from itertools import count

from tests.forecast_fixtures import (
    IST,
    atm_strike,
    by_strike,
    chain,
    forecast_config,
    ist,
    price_path,
    produced,
    session_bars,
    spot_of,
    underlying,
    weekdays_before,
)
from trading.domain.contracts.forecast import ForecastEventKind
from trading.domain.enums import FamilyId, ModeId, OptionType, ReasonCode
from trading.forecast.bars import Bar
from trading.forecast.inputs import ForecastInputs
from trading.forecast.m2 import m2_view
from trading.forecast.reference import PremarketCues, ReferenceInputs
from trading.runtime.forecast_stage import ForecastCycleInputs, ForecastStage
from trading.runtime.session_routing import ProducedFamilyRequest

TODAY = date(2026, 9, 16)
HISTORY = weekdays_before(TODAY, 25)


def _session(direction: int) -> tuple[tuple[Bar, ...], Decimal]:
    """25 trending sessions, then an opening range and a confirmed breakout."""
    slope = Decimal("0.25") * direction
    closes = price_path(len(HISTORY) * 75, start=Decimal(23500), slope=slope)
    base = closes[-1]
    step = Decimal("1.5") * direction
    today = [base + 2, base - 2, base + 3]
    today += [base + 3 + step * i for i in range(1, 16)]
    today.append(today[-1] + Decimal(40) * direction)
    bars = session_bars([*HISTORY, TODAY], [*closes, *today])
    return bars, today[-1]


def _inputs(direction: int = 1, **overrides: object) -> ForecastInputs:
    bars, spot = _session(direction)
    values: dict[str, object] = {
        "as_of": bars[-1].start + timedelta(minutes=5),
        "zone": IST,
        "spot": spot,
        "bars": bars,
        "current_vix": Decimal("14"),
        "previous_vix": Decimal("13.8"),
        **overrides,
    }
    return ForecastInputs(**values)  # type: ignore[arg-type]


class TestM2View:
    def test_confirmed_upside_breakout_is_bullish(self) -> None:
        """Range-expansion-confirmed ORB plus trend produces an up view."""
        view = m2_view(_inputs(), forecast_config())
        assert view.features["orb_break_confirmed"] == 1
        assert view.features["prior_day_break"] == 1
        assert view.direction == 1
        assert view.event is ForecastEventKind.UP_MOVE
        assert view.p_up > Decimal("0.6")

    def test_downside_mirror_is_bearish(self) -> None:
        view = m2_view(_inputs(direction=-1), forecast_config())
        assert view.features["orb_break_confirmed"] == -1
        assert view.direction == -1
        assert view.p_up < Decimal("0.4")

    def test_invalidation_sits_below_the_broken_level(self) -> None:
        """Exit level is the broken prior high minus an ATR buffer, under spot."""
        inputs = _inputs()
        view = m2_view(inputs, forecast_config())
        assert view.invalidation_below is not None
        assert view.invalidation_below < inputs.spot

    def test_missing_premarket_is_absent_not_zero(self) -> None:
        """Absent cues never masquerade as neutral evidence."""
        view = m2_view(_inputs(), forecast_config())
        assert "premarket_bias" in view.absent
        assert "premarket_bias" not in view.features

    def test_stale_premarket_cue_is_ignored(self) -> None:
        """Yesterday's GIFT Nifty move is not today's pre-market prior."""
        stale = PremarketCues(
            as_of=ist(TODAY - timedelta(days=1), 8, 45),
            gift_nifty_change=Decimal("0.01"),
        )
        view = m2_view(
            _inputs(reference=ReferenceInputs(premarket=stale)), forecast_config()
        )
        assert "premarket_bias" in view.absent

    def test_bearish_premarket_prior_lowers_p_up(self) -> None:
        """A weak overnight session pulls the breakout probability down."""
        cue = PremarketCues(as_of=ist(TODAY, 8, 45), gift_nifty_change=Decimal("-0.02"))
        base = m2_view(_inputs(), forecast_config())
        weak = m2_view(
            _inputs(reference=ReferenceInputs(premarket=cue)), forecast_config()
        )
        assert weak.features["premarket_bias"] == Decimal("-0.02")
        assert weak.p_up < base.p_up

    def test_no_session_bars_fails_to_neutral(self) -> None:
        """Without today's bars the opening range is unknown, not guessed."""
        inputs = _inputs()
        yesterday_only = tuple(
            bar for bar in inputs.bars if bar.start.astimezone(IST).date() != TODAY
        )
        view = m2_view(_inputs(bars=yesterday_only), forecast_config())
        assert "session_levels" in view.absent
        assert "orb_break" not in view.features


def _stage_ctx(inputs: ForecastInputs) -> ForecastCycleInputs:
    spot = inputs.spot
    centre = int((spot / 50).to_integral_value()) * 50
    options = chain(
        spot=spot,
        centre=centre,
        dte=7,
        expiry=TODAY + timedelta(days=7),
        at=inputs.as_of,
    )
    return ForecastCycleInputs(
        cycle_id="FCY-1",
        as_of=inputs.as_of,
        zone=IST,
        underlying=underlying(spot, inputs.as_of),
        bars=inputs.bars,
        market=None,
        p1=None,
        chain=options,
        instruments={},
        charges_per_lot_leg=Decimal(0),
        max_spread_fraction=Decimal("0.05"),
        min_open_interest=1000,
        vix_history=(Decimal("13.8"), Decimal("14")),
    )


def _stage(**m2: object) -> ForecastStage:
    ids = count(1)
    return ForecastStage(
        forecast_config(m2=m2),
        config_version="test",
        new_id=lambda prefix: f"{prefix}-{next(ids)}",
    )


def _families(ctx: ForecastCycleInputs) -> list[ProducedFamilyRequest]:
    centre = atm_strike(spot_of(ctx))
    call = by_strike(ctx.chain, centre, OptionType.CALL)
    put = by_strike(ctx.chain, centre, OptionType.PUT)
    return [
        produced(ModeId.M2_DIRECTIONAL, FamilyId.long_call, (call,)),
        produced(ModeId.M2_DIRECTIONAL, FamilyId.long_put, (put,)),
    ]


class TestM2Gate:
    def test_enforced_gate_shadows_the_contrary_family(self) -> None:
        """A bearish structure against a bullish M2 view does not execute."""
        ctx = _stage_ctx(_inputs())
        result = _stage(enforce=True).evaluate(_families(ctx), ctx)
        call, put = result.produced
        assert not put.execute
        assert ReasonCode.FORECAST_DIRECTION_CONFLICT in put.bound.binding.reason_codes
        assert ReasonCode.FORECAST_DIRECTION_CONFLICT not in (
            call.bound.binding.reason_codes
        )
        assert result.directions[(ModeId.M2_DIRECTIONAL, FamilyId.long_call)] == 1

    def test_measurement_mode_never_changes_execution(self) -> None:
        """Default config records the verdict but leaves trading untouched."""
        ctx = _stage_ctx(_inputs())
        families = _families(ctx)
        result = _stage().evaluate(families, ctx)
        assert result.produced == tuple(families)
        assert result.directions == {}
        put_forecast = next(
            item for item in result.forecasts if item.family_id == "long_put"
        )
        assert not put_forecast.gate_passed
        assert not put_forecast.gate_enforced

    def test_forecast_value_exceeds_implied_for_the_right_side(self) -> None:
        """The edge test compares the mode's distribution to the market's."""
        ctx = _stage_ctx(_inputs())
        result = _stage().evaluate(_families(ctx), ctx)
        call = next(item for item in result.forecasts if item.family_id == "long_call")
        put = next(item for item in result.forecasts if item.family_id == "long_put")
        assert call.forecast_value is not None and call.implied_value is not None
        assert call.forecast_value > call.implied_value
        assert put.forecast_value is not None and put.implied_value is not None
        assert put.forecast_value < put.implied_value
        assert call.p_profit is not None and call.p_profit_implied is not None
        assert call.p_profit > call.p_profit_implied

    def test_enforced_exits_attach_nifty_invalidation(self) -> None:
        """A long call exits when NIFTY falls back through the broken level."""
        inputs = _inputs()
        ctx = _stage_ctx(inputs)
        result = _stage(enforce_exits=True).evaluate(_families(ctx), ctx)
        overlay = result.overlays[(ModeId.M2_DIRECTIONAL, FamilyId.long_call)]
        view = result.views[ModeId.M2_DIRECTIONAL]
        assert overlay.underlying_stop_below is not None
        assert view.invalidation_below is not None
        assert overlay.underlying_stop_below < inputs.spot
        assert overlay.underlying_stop_above is None
        assert overlay.time_exit is None
