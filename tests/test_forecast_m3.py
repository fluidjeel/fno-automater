"""M3 swing view, IV-driven structure choice, forecast strikes and invalidation."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timedelta
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
from trading.domain.contracts import FeatureSnapshot
from trading.domain.enums import FamilyId, ModeId, OptionType, ReasonCode
from trading.forecast.bars import Bar
from trading.forecast.inputs import ForecastInputs
from trading.forecast.m3 import m3_view, swing_pivots
from trading.forecast.structures import StrikeSuggestion, suggest_vertical
from trading.runtime.forecast_stage import ForecastCycleInputs, ForecastStage
from trading.runtime.session_routing import ProducedFamilyRequest

TODAY = date(2026, 9, 16)
HISTORY = weekdays_before(TODAY, 25)
EXPIRY = TODAY + timedelta(days=9)


def _bars(direction: int = 1) -> tuple[Bar, ...]:
    """A trending swing: three-hour waves riding a steady trend."""
    closes = price_path(
        len(HISTORY) * 75,
        start=Decimal(24000),
        slope=Decimal("0.3") * direction,
        wave=Decimal(4),
        period=36,
        noise=Decimal(1),
    )
    return session_bars(HISTORY, closes)


def _inputs(direction: int = 1, **overrides: object) -> ForecastInputs:
    bars = _bars(direction)
    values: dict[str, object] = {
        "as_of": ist(TODAY, 10, 45),
        "zone": IST,
        "spot": bars[-1].close,
        "bars": bars,
        "current_vix": Decimal(14),
        "previous_vix": Decimal(14),
        **overrides,
    }
    return ForecastInputs(**values)  # type: ignore[arg-type]


class TestSwingView:
    def test_pivots_are_strict_three_bar_extremes(self) -> None:
        bars = _bars()[:60]
        highs, lows = swing_pivots(bars)
        for pivot in highs:
            index = bars.index(pivot)
            assert bars[index - 1].high < pivot.high > bars[index + 1].high
        for pivot in lows:
            index = bars.index(pivot)
            assert bars[index - 1].low > pivot.low < bars[index + 1].low

    def test_higher_highs_and_lows_are_bullish(self) -> None:
        view = m3_view(_inputs(), forecast_config())
        assert view.features["swing_structure"] == 1
        assert view.direction == 1

    def test_lower_highs_and_lows_are_bearish(self) -> None:
        view = m3_view(_inputs(direction=-1), forecast_config())
        assert view.features["swing_structure"] == -1
        assert view.direction == -1

    def test_invalidation_is_the_last_swing_low_less_a_buffer(self) -> None:
        """A bull swing is wrong once NIFTY breaks the swing that defined it."""
        inputs = _inputs()
        view = m3_view(inputs, forecast_config())
        assert view.invalidation_below is not None
        assert view.invalidation_below < inputs.spot
        assert view.invalidation_above is not None


class TestStructureChoice:
    def test_cheap_iv_buys_the_debit_spread(self) -> None:
        view = m3_view(_inputs(iv_percentile=Decimal(30)), forecast_config())
        assert view.view == "DEBIT"
        assert view.allowed_families == frozenset({FamilyId.bull_call_debit})

    def test_rich_iv_sells_the_credit_spread(self) -> None:
        view = m3_view(_inputs(iv_percentile=Decimal(70)), forecast_config())
        assert view.view == "CREDIT"
        assert view.allowed_families == frozenset({FamilyId.bull_put_credit})

    def test_middling_iv_allows_either(self) -> None:
        view = m3_view(_inputs(iv_percentile=Decimal(50)), forecast_config())
        assert view.view == "EITHER"
        assert view.allowed_families == frozenset(
            {FamilyId.bull_call_debit, FamilyId.bull_put_credit}
        )

    def test_unknown_iv_percentile_is_recorded_absent(self) -> None:
        view = m3_view(_inputs(), forecast_config())
        assert "iv_percentile" in view.absent
        assert view.view == "EITHER"


def _chain(spot: Decimal, at: datetime | None = None) -> tuple[FeatureSnapshot, ...]:
    return chain(
        spot=spot,
        centre=atm_strike(spot),
        dte=9,
        expiry=EXPIRY,
        at=at,
    )


def _strike(item: FeatureSnapshot) -> Decimal:
    assert item.contract.strike is not None
    return item.contract.strike


class TestForecastStrikes:
    def _suggest(
        self,
        family: FamilyId,
        *,
        tradable: Callable[[FeatureSnapshot], bool] | None = None,
    ) -> tuple[StrikeSuggestion | None, Decimal]:
        inputs = _inputs()
        view = m3_view(inputs, forecast_config())
        mode = forecast_config().m3
        suggestion = suggest_vertical(
            family,
            _chain(inputs.spot),
            spot=inputs.spot,
            distribution=view.distribution,
            expiry_days=9,
            credit_short_quantile=mode.credit_short_quantile,
            debit_target_quantile=mode.debit_target_quantile,
            credit_width_points=mode.credit_width_points,
            tradable=tradable or (lambda _item: True),
        )
        return suggestion, inputs.spot

    def test_debit_buys_at_the_money_and_sells_at_the_target(self) -> None:
        """Long ATM, short at the 65th percentile of the forecast distribution."""
        suggestion, spot = self._suggest(FamilyId.bull_call_debit)
        assert suggestion is not None
        long_strike = _strike(suggestion.long_leg)
        short_strike = _strike(suggestion.short_leg)
        assert long_strike == atm_strike(spot)
        assert short_strike > long_strike
        assert abs(short_strike - suggestion.short_level) <= 25

    def test_credit_short_strike_sits_outside_the_forecast_quantile(self) -> None:
        """The short put has at most a 20% forecast chance of finishing in the money."""
        suggestion, _spot = self._suggest(FamilyId.bull_put_credit)
        assert suggestion is not None
        short_strike = _strike(suggestion.short_leg)
        long_strike = _strike(suggestion.long_leg)
        assert short_strike <= suggestion.short_level
        assert short_strike > suggestion.short_level - 50
        assert short_strike - long_strike >= 200

    def test_untradable_strikes_are_skipped(self) -> None:
        """Wide or thin strikes never become a suggested leg."""
        _first, spot = self._suggest(FamilyId.bull_call_debit)
        banned = Decimal(atm_strike(spot))
        suggestion, _ = self._suggest(
            FamilyId.bull_call_debit,
            tradable=lambda item: item.contract.strike != banned,
        )
        assert suggestion is not None
        assert suggestion.long_leg.contract.strike != banned


def _stage(**m3: object) -> ForecastStage:
    ids = count(1)
    return ForecastStage(
        forecast_config(m3=m3),
        config_version="test",
        new_id=lambda prefix: f"{prefix}-{next(ids)}",
    )


def _ctx(hour: int = 10, minute: int = 45) -> ForecastCycleInputs:
    bars = _bars()
    spot = bars[-1].close
    as_of = ist(TODAY, hour, minute)
    return ForecastCycleInputs(
        cycle_id="FCY-3",
        as_of=as_of,
        zone=IST,
        underlying=underlying(spot, as_of),
        bars=bars,
        market=None,
        p1=None,
        chain=_chain(spot, as_of),
        instruments={},
        charges_per_lot_leg=Decimal(0),
        max_spread_fraction=Decimal("0.05"),
        min_open_interest=1000,
        vix_history=(Decimal(14), Decimal(14)),
    )


def _binder_vertical(
    ctx: ForecastCycleInputs, family: FamilyId
) -> ProducedFamilyRequest:
    spot = spot_of(ctx)
    kind = OptionType.CALL if family is FamilyId.bull_call_debit else OptionType.PUT
    offset = 1 if kind is OptionType.CALL else -1
    first = by_strike(ctx.chain, atm_strike(spot) - 300 * offset, kind)
    second = by_strike(ctx.chain, atm_strike(spot) - 200 * offset, kind)
    return produced(ModeId.M3_TACTICAL_POSITIONAL, family, (first, second))


class TestM3Stage:
    def test_enforced_mode_replaces_binder_strikes_with_forecast_strikes(self) -> None:
        ctx = _ctx()
        result = _stage(enforce=True).evaluate(
            [_binder_vertical(ctx, FamilyId.bull_call_debit)],
            ctx,
        )
        bound = result.produced[0].bound
        spot = spot_of(ctx)
        assert bound.candidates[0].contract.strike == atm_strike(spot)
        assert bound.binding.selected_symbols == tuple(
            item.contract.symbol for item in bound.candidates
        )

    def test_measurement_mode_keeps_binder_strikes(self) -> None:
        ctx = _ctx()
        family = _binder_vertical(ctx, FamilyId.bull_call_debit)
        result = _stage().evaluate([family], ctx)
        assert result.produced[0] is family

    def test_outside_entry_window_is_refused(self) -> None:
        """M3 enters only in its validated windows, not on midday noise."""
        ctx = _ctx(12, 0)
        result = _stage(enforce=True).evaluate(
            [_binder_vertical(ctx, FamilyId.bull_call_debit)],
            ctx,
        )
        assert not result.produced[0].execute
        assert (
            ReasonCode.FORECAST_OUTSIDE_ENTRY_WINDOW
            in result.produced[0].bound.binding.reason_codes
        )

    def test_credit_exit_sits_just_inside_the_short_strike(self) -> None:
        """A credit spread exits on NIFTY near its short strike, with a thesis clock."""
        ctx = _ctx()
        result = _stage(enforce_exits=True).evaluate(
            [_binder_vertical(ctx, FamilyId.bull_put_credit)],
            ctx,
        )
        overlay = result.overlays[
            (ModeId.M3_TACTICAL_POSITIONAL, FamilyId.bull_put_credit)
        ]
        short_strike = result.produced[0].bound.candidates[1].contract.strike
        assert short_strike is not None
        assert overlay.underlying_stop_below is not None
        expected = short_strike * Decimal("1.003")
        assert abs(overlay.underlying_stop_below - expected) < Decimal("0.01")
        assert overlay.time_exit is not None
        local = overlay.time_exit.astimezone(IST)
        assert local.date() == TODAY + timedelta(days=7)
        assert (local.hour, local.minute) == (15, 20)
