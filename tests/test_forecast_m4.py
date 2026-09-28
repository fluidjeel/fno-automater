"""M4 weekly regime: direction x volatility map, events, caching and basket delta."""

from __future__ import annotations

from dataclasses import replace
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
from trading.domain.enums import FamilyId, ModeId, OptionType, ReasonCode
from trading.forecast.bars import Bar
from trading.forecast.inputs import ForecastInputs
from trading.forecast.m4 import (
    M4_LONG_VOL,
    M4_SHORT_VOL,
    m4_regime,
    m4_regime_is_stale,
    m4_view,
)
from trading.forecast.reference import CalendarEvent, ReferenceInputs
from trading.runtime.forecast_stage import ForecastCycleInputs, ForecastStage, OpenLeg
from trading.runtime.session_routing import ProducedFamilyRequest

TODAY = date(2026, 9, 16)
HISTORY = weekdays_before(TODAY, 55)
FRONT = date(2026, 9, 18)
BACK = date(2026, 9, 24)


def _bars(
    *, slope: Decimal, noise: Decimal = Decimal(2), wave: Decimal = Decimal(0)
) -> tuple[Bar, ...]:
    closes = price_path(
        len(HISTORY) * 75,
        start=Decimal(24000),
        slope=slope,
        noise=noise,
        wave=wave,
        period=50,
    )
    return session_bars(HISTORY, closes)


def _inputs(
    *,
    slope: Decimal = Decimal(0),
    noise: Decimal = Decimal(2),
    vix: Decimal = Decimal(14),
    bars: tuple[Bar, ...] | None = None,
    **overrides: object,
) -> ForecastInputs:
    history = bars if bars is not None else _bars(slope=slope, noise=noise)
    values: dict[str, object] = {
        "as_of": ist(TODAY, 9, 50),
        "zone": IST,
        "spot": history[-1].close,
        "bars": history,
        "current_vix": vix,
        "previous_vix": vix,
        **overrides,
    }
    return ForecastInputs(**values)  # type: ignore[arg-type]


_KNOWN_QUIET = ReferenceInputs(events_loaded=True)


class TestWeeklyRegimeMap:
    def test_trend_with_rich_vol_sells_premium_directionally(self) -> None:
        """Bull trend with IV above forecast RV maps to the bull credit spread."""
        regime = m4_regime(_inputs(slope=Decimal(1)), forecast_config())
        assert regime.view == "DIRECTIONAL"
        assert regime.vol_ratio is not None and regime.vol_ratio > 1
        assert regime.allowed_families == frozenset({FamilyId.bull_put_credit})

    def test_trend_with_cheap_vol_buys_the_debit_spread(self) -> None:
        regime = m4_regime(
            _inputs(slope=Decimal(1), vix=Decimal("0.01")), forecast_config()
        )
        assert regime.view == "DIRECTIONAL"
        assert regime.vol_ratio is not None and regime.vol_ratio < 1
        assert regime.allowed_families == frozenset({FamilyId.bull_call_debit})

    def test_range_with_rich_vol_and_known_calendar_is_short_vol(self) -> None:
        """Short vol needs a directionless view, rich IV and a known calendar."""
        regime = m4_regime(_inputs(reference=_KNOWN_QUIET), forecast_config())
        assert regime.view == "SHORT_VOL"
        assert regime.allowed_families == M4_SHORT_VOL
        view = m4_view(regime, forecast_config())
        assert view.allowed_families == M4_SHORT_VOL

    def test_unknown_calendar_never_sells_vol(self) -> None:
        """An unloaded event calendar is unknown risk, not an empty week."""
        regime = m4_regime(_inputs(), forecast_config())
        assert "event_calendar" in regime.absent
        assert regime.view != "SHORT_VOL"

    def test_realized_above_implied_buys_vol(self) -> None:
        regime = m4_regime(
            _inputs(noise=Decimal(60), reference=_KNOWN_QUIET), forecast_config()
        )
        assert regime.vol_ratio is not None
        assert regime.vol_ratio <= Decimal("0.90")
        assert regime.view == "LONG_VOL"
        assert regime.allowed_families == M4_LONG_VOL


def _event_reference(
    back_iv: Decimal,
) -> tuple[ReferenceInputs, tuple[tuple[date, Decimal], ...]]:
    event = CalendarEvent(
        event_date=date(2026, 9, 22),
        kind="POLICY",
        importance=3,
        typical_move_fraction=Decimal("0.01"),
    )
    reference = ReferenceInputs(events=(event,), events_loaded=True)
    return reference, ((FRONT, Decimal(14)), (BACK, back_iv))


class TestEventPricing:
    def test_richly_priced_event_still_allows_short_vol(self) -> None:
        """The front/back IV gap prices the event at least at its history."""
        reference, ivs = _event_reference(Decimal(16))
        regime = m4_regime(
            _inputs(reference=reference, atm_iv_by_expiry=ivs), forecast_config()
        )
        assert regime.event_richness is not None and regime.event_richness >= 1
        assert regime.view == "SHORT_VOL"

    def test_cheap_event_buys_vol_instead_of_selling_it(self) -> None:
        """An event priced below its typical move is long-vol, never short."""
        reference, ivs = _event_reference(Decimal("14.2"))
        regime = m4_regime(
            _inputs(reference=reference, atm_iv_by_expiry=ivs), forecast_config()
        )
        assert regime.event_richness is not None
        assert regime.event_richness <= Decimal("0.80")
        assert regime.view == "LONG_VOL"


class TestRegimeCadence:
    def test_regime_holds_within_the_week(self) -> None:
        """Intraday noise does not re-decide a weeks-horizon book."""
        inputs = _inputs()
        regime = m4_regime(inputs, forecast_config())
        later = replace(inputs, as_of=inputs.as_of + timedelta(hours=4))
        assert not m4_regime_is_stale(regime, later, forecast_config())

    def test_new_iso_week_rebuilds(self) -> None:
        inputs = _inputs()
        regime = m4_regime(inputs, forecast_config())
        next_week = replace(inputs, as_of=inputs.as_of + timedelta(days=7))
        assert m4_regime_is_stale(regime, next_week, forecast_config())

    def test_vix_shock_rebuilds(self) -> None:
        inputs = _inputs()
        regime = m4_regime(inputs, forecast_config())
        shocked = replace(inputs, current_vix=Decimal(17))
        assert m4_regime_is_stale(regime, shocked, forecast_config())

    def test_opening_gap_rebuilds(self) -> None:
        """A gap of several daily sigmas since the regime was built forces a rebuild."""
        history = _bars(slope=Decimal(0), wave=Decimal(3))
        yesterday = replace(_inputs(bars=history), as_of=ist(HISTORY[-1], 15, 25))
        regime = m4_regime(yesterday, forecast_config())
        last = history[-1].close
        gap_open = last * Decimal("1.03")
        first = Bar(
            start=ist(TODAY, 9, 15),
            open=gap_open,
            high=gap_open + 5,
            low=gap_open - 5,
            close=gap_open,
            volume=Decimal(1000),
        )
        today = _inputs(bars=(*history, first), as_of=ist(TODAY, 9, 25))
        assert m4_regime_is_stale(regime, today, forecast_config())
        calm = replace(first, open=last, high=last + 5, low=last - 5, close=last)
        assert not m4_regime_is_stale(
            regime,
            _inputs(bars=(*history, calm), as_of=ist(TODAY, 9, 25)),
            forecast_config(),
        )


def _stage_ctx(open_legs: tuple[OpenLeg, ...]) -> ForecastCycleInputs:
    bars = _bars(slope=Decimal(1))
    spot = bars[-1].close
    centre = int((spot / 50).to_integral_value()) * 50
    as_of = ist(TODAY, 10, 0)
    options = chain(
        spot=spot, centre=centre, dte=21, expiry=TODAY + timedelta(days=21), at=as_of
    )
    return ForecastCycleInputs(
        cycle_id="FCY-4",
        as_of=as_of,
        zone=IST,
        underlying=underlying(spot, as_of),
        bars=bars,
        market=None,
        p1=None,
        chain=options,
        instruments={},
        charges_per_lot_leg=Decimal(0),
        max_spread_fraction=Decimal("0.05"),
        min_open_interest=1000,
        vix_history=(Decimal(14), Decimal(14)),
        open_legs=open_legs,
    )


def _stage() -> ForecastStage:
    ids = count(1)
    return ForecastStage(
        forecast_config(),
        config_version="test",
        new_id=lambda prefix: f"{prefix}-{next(ids)}",
    )


def _bull_put_credit(ctx: ForecastCycleInputs) -> ProducedFamilyRequest:
    centre = atm_strike(spot_of(ctx))
    short = by_strike(ctx.chain, centre - 100, OptionType.PUT)
    long = by_strike(ctx.chain, centre - 200, OptionType.PUT)
    return produced(
        ModeId.M4_STRATEGIC_POSITIONAL, FamilyId.bull_put_credit, (long, short)
    )


def _family_reasons(ctx: ForecastCycleInputs) -> tuple[ReasonCode, ...]:
    result = _stage().evaluate([_bull_put_credit(ctx)], ctx)
    forecast = next(item for item in result.forecasts if item.family_id is not None)
    return forecast.reason_codes


class TestBasketDelta:
    def test_structure_within_budget_passes_the_basket_check(self) -> None:
        reasons = _family_reasons(_stage_ctx(()))
        assert ReasonCode.FORECAST_BASKET_LIMIT not in reasons

    def test_structure_adding_to_an_over_budget_book_is_refused(self) -> None:
        """Net M4 delta past its budget blocks structures that add to it."""
        base = _stage_ctx(())
        centre = atm_strike(spot_of(base))
        call = by_strike(base.chain, centre, OptionType.CALL)
        book = (
            OpenLeg(
                symbol=call.contract.symbol,
                mode_id=ModeId.M4_STRATEGIC_POSITIONAL,
                signed_units=Decimal(400),
            ),
        )
        reasons = _family_reasons(_stage_ctx(book))
        assert ReasonCode.FORECAST_BASKET_LIMIT in reasons

    def test_other_modes_do_not_count_against_the_m4_budget(self) -> None:
        base = _stage_ctx(())
        centre = atm_strike(spot_of(base))
        call = by_strike(base.chain, centre, OptionType.CALL)
        book = (
            OpenLeg(
                symbol=call.contract.symbol,
                mode_id=ModeId.M2_DIRECTIONAL,
                signed_units=Decimal(400),
            ),
        )
        reasons = _family_reasons(_stage_ctx(book))
        assert ReasonCode.FORECAST_BASKET_LIMIT not in reasons
