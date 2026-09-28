"""M1 order-flow forecaster, cost hurdle and signal-decay exits."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime, timedelta
from decimal import Decimal
from itertools import count
from pathlib import Path

import pytest

import tests.factories as f
from tests.forecast_fixtures import (
    IST,
    forecast_config,
    ist,
    option,
    price_path,
    produced,
    session_bars,
    underlying,
    weekdays_before,
)
from tests.test_paper_lifecycle import _open_long, _option_snapshot
from trading.domain.clock import FrozenClock
from trading.domain.contracts import FeatureSnapshot
from trading.domain.enums import (
    DataQuality,
    FamilyId,
    ModeId,
    OptionType,
    ReasonCode,
    TradeState,
)
from trading.forecast.inputs import ForecastInputs, OrderFlowStats
from trading.forecast.m1 import m1_cost_hurdle, m1_time_bucket, m1_view
from trading.forecast.order_flow import (
    OrderFlowTracker,
    Quote,
    microprice,
    ofi_increment,
)
from trading.runtime.forecast_stage import ForecastCycleInputs, ForecastStage
from trading.storage.trading_store import TradingStore

TODAY = date(2026, 9, 16)
T0 = ist(TODAY, 10, 0)


def _quote(seconds: int, bid: str, bid_size: int, ask: str, ask_size: int) -> Quote:
    return Quote(
        at=T0 + timedelta(seconds=seconds),
        bid=Decimal(bid),
        bid_size=Decimal(bid_size),
        ask=Decimal(ask),
        ask_size=Decimal(ask_size),
    )


class TestOrderFlow:
    def test_bid_lift_is_buying_pressure(self) -> None:
        """A higher bid adds its full size as buy-side flow."""
        before = _quote(0, "100", 500, "101", 500)
        after = _quote(1, "100.5", 400, "101", 500)
        assert ofi_increment(before, after) > 0

    def test_ask_drop_is_selling_pressure(self) -> None:
        before = _quote(0, "100", 500, "101", 500)
        after = _quote(1, "100", 500, "100.5", 400)
        assert ofi_increment(before, after) < 0

    def test_microprice_leans_toward_the_thin_side(self) -> None:
        """Heavy bids with thin offers point the fair price toward the ask."""
        heavy_bid = _quote(0, "100", 900, "101", 100)
        assert microprice(heavy_bid) > Decimal("100.5")

    def test_call_buying_and_put_selling_read_bullish(self) -> None:
        tracker = OrderFlowTracker(short_seconds=60, long_seconds=300)
        for step in range(6):
            lift = Decimal(step) / 10
            tracker.observe(
                "CALL",
                _quote(step * 10, str(100 + lift), 600, str(101 + lift), 300),
            )
            tracker.observe(
                "PUT",
                _quote(step * 10, str(100 - lift), 300, str(101 - lift), 600),
            )
        stats = tracker.stats(T0 + timedelta(seconds=55))
        assert stats.ofi_short is not None and stats.ofi_short > 0
        assert stats.microprice_drift_bps is not None and stats.microprice_drift_bps > 0
        assert stats.samples == 12

    def test_stale_and_out_of_order_quotes_are_ignored(self) -> None:
        """A replayed or crossed quote never creates phantom flow."""
        tracker = OrderFlowTracker(short_seconds=60, long_seconds=300)
        tracker.observe("CALL", _quote(10, "100", 500, "101", 500))
        tracker.observe("CALL", _quote(5, "90", 500, "91", 500))
        tracker.observe("CALL", _quote(20, "101", 500, "100", 500))
        stats = tracker.stats(T0 + timedelta(seconds=30))
        assert stats.samples == 1
        assert stats.ofi_short is None


_HISTORY = weekdays_before(TODAY, 3)


def _inputs(
    flow: OrderFlowStats | None, *, at: datetime = T0, **extra: object
) -> ForecastInputs:
    closes = price_path(len(_HISTORY) * 75, start=Decimal(24000), slope=Decimal(0))
    bars = session_bars(_HISTORY, closes)
    values: dict[str, object] = {
        "as_of": at,
        "zone": IST,
        "spot": closes[-1],
        "bars": bars,
        "current_vix": Decimal(14),
        "order_flow": flow,
        **extra,
    }
    return ForecastInputs(**values)  # type: ignore[arg-type]


_BULL_FLOW = OrderFlowStats(
    ofi_short=Decimal(2),
    ofi_long=Decimal(1),
    microprice_drift_bps=Decimal(5),
    samples=20,
)


class TestM1View:
    def test_positive_flow_is_an_up_view(self) -> None:
        view = m1_view(_inputs(_BULL_FLOW), forecast_config())
        assert view.direction == 1
        assert view.p_up > Decimal("0.7")
        assert view.features["bucket_morning"] == 1

    def test_missing_flow_is_absent_input(self) -> None:
        """No order flow means no M1 opinion, never a neutral trade signal."""
        view = m1_view(_inputs(None), forecast_config())
        assert ReasonCode.FORECAST_INPUT_ABSENT in view.reason_codes

    def test_futures_lead_adds_to_the_score(self) -> None:
        lagged = m1_view(
            _inputs(_BULL_FLOW, underlying_features={"futures_lead_bps": Decimal(20)}),
            forecast_config(),
        )
        base = m1_view(_inputs(_BULL_FLOW), forecast_config())
        assert lagged.p_up > base.p_up

    def test_after_the_close_is_outside_the_window(self) -> None:
        view = m1_view(_inputs(_BULL_FLOW, at=ist(TODAY, 15, 35)), forecast_config())
        assert ReasonCode.FORECAST_OUTSIDE_ENTRY_WINDOW in view.reason_codes

    def test_blocked_bucket_is_refused(self) -> None:
        """Buckets the calibration report shows lose money can be switched off."""
        config = forecast_config(m1={"blocked_time_buckets": ("morning",)})
        view = m1_view(_inputs(_BULL_FLOW), config)
        assert ReasonCode.FORECAST_OUTSIDE_ENTRY_WINDOW in view.reason_codes

    def test_time_buckets_cover_the_session(self) -> None:
        assert m1_time_bucket(ist(TODAY, 9, 15).astimezone(IST).time()) == "open"
        assert m1_time_bucket(ist(TODAY, 15, 29).astimezone(IST).time()) == "close"
        assert m1_time_bucket(ist(TODAY, 9, 0).astimezone(IST).time()) is None


class TestCostHurdle:
    def test_expected_move_must_beat_a_multiple_of_friction(self) -> None:
        """A minutes-horizon trade needs premium motion well above round-trip cost."""
        ok, expected = m1_cost_hurdle(
            spot=Decimal(24000),
            sigma=Decimal("0.002"),
            delta=Decimal("0.5"),
            gamma=None,
            spread=Decimal(1),
            cost_per_unit=Decimal("0.5"),
            multiple=Decimal("2.5"),
        )
        assert ok
        assert expected == Decimal("0.5") * Decimal(48) * Decimal("0.7978845608028654")

    def test_wide_spread_fails_the_hurdle(self) -> None:
        ok, _expected = m1_cost_hurdle(
            spot=Decimal(24000),
            sigma=Decimal("0.0005"),
            delta=Decimal("0.5"),
            gamma=None,
            spread=Decimal(4),
            cost_per_unit=Decimal(1),
            multiple=Decimal("2.5"),
        )
        assert not ok


def _stage(**m1: object) -> ForecastStage:
    ids = count(1)
    return ForecastStage(
        forecast_config(m1=m1),
        config_version="test",
        new_id=lambda prefix: f"{prefix}-{next(ids)}",
    )


def _ctx(
    at: datetime, *, call_bid_lift: Decimal, half_spread: Decimal = Decimal("0.5")
) -> ForecastCycleInputs:
    closes = price_path(len(_HISTORY) * 75, start=Decimal(24000), slope=Decimal(0))
    bars = session_bars(_HISTORY, closes)
    spot = closes[-1]
    centre = int((spot / 50).to_integral_value()) * 50
    expiry = TODAY + timedelta(days=2)
    call = option(
        centre,
        OptionType.CALL,
        spot=spot + call_bid_lift,
        dte=2,
        expiry=expiry,
        half_spread=half_spread,
        at=at,
    )
    put = option(
        centre,
        OptionType.PUT,
        spot=spot + call_bid_lift,
        dte=2,
        expiry=expiry,
        half_spread=half_spread,
        at=at,
    )
    return ForecastCycleInputs(
        cycle_id="FCY-1",
        as_of=at,
        zone=IST,
        underlying=underlying(spot, at),
        bars=bars,
        market=None,
        p1=None,
        chain=(call, put),
        instruments={},
        charges_per_lot_leg=Decimal(0),
        max_spread_fraction=Decimal("0.05"),
        min_open_interest=1000,
        vix_history=(Decimal(14), Decimal(14)),
    )


def _drive(stage: ForecastStage, direction: int) -> ForecastCycleInputs:
    """Two cycles in which NIFTY-linked premiums move ``direction``."""
    ctx = _ctx(T0, call_bid_lift=Decimal(0))
    stage.evaluate([], ctx)
    ctx = _ctx(T0 + timedelta(seconds=30), call_bid_lift=Decimal(20) * direction)
    stage.evaluate([], ctx)
    return ctx


class TestSignalDecay:
    def test_no_view_yet_never_forces_an_exit(self) -> None:
        assert not _stage().m1_signal_decayed(1)

    def test_decay_tracks_the_latest_flow(self) -> None:
        """A long thesis survives bullish flow and decays once flow turns."""
        stage = _stage()
        _drive(stage, 1)
        assert not stage.m1_signal_decayed(1)
        assert stage.m1_signal_decayed(-1)
        bearish = _stage()
        _drive(bearish, -1)
        assert bearish.m1_signal_decayed(1)

    def test_wide_spread_fails_the_enforced_cost_hurdle(self) -> None:
        stage = _stage(enforce=True)
        ctx = _drive(stage, 1)
        wide = _ctx(
            ctx.as_of + timedelta(seconds=30),
            call_bid_lift=Decimal(20),
            half_spread=Decimal(30),
        )
        call = next(
            item for item in wide.chain if item.contract.option_type is OptionType.CALL
        )
        result = stage.evaluate(
            [produced(ModeId.M1_CAS, FamilyId.long_call, (call,))], wide
        )
        assert not result.produced[0].execute
        assert ReasonCode.FORECAST_COST_HURDLE in (
            result.produced[0].bound.binding.reason_codes
        )


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(f.NOW + timedelta(seconds=60))


@pytest.fixture
def store(tmp_path: Path, clock: FrozenClock) -> Iterator[TradingStore]:
    trading_store = TradingStore.open(tmp_path / "paper.sqlite", clock=clock)
    yield trading_store
    trading_store.close()


def _exit_quote(contract: object, clock: FrozenClock) -> FeatureSnapshot:
    now = clock.now_utc()
    return _option_snapshot(
        contract,
        market=f.quote(bid=f.price("49.00"), ask=f.price("49.05")),
        times=f.snapshot_times(
            event_time=now, source_time=now, receive_time=now, calculation_time=now
        ),
        quality=f.quality(state=DataQuality.VALID, reason_codes=(ReasonCode.OK,)),
    )


class TestRunnerDecayExit:
    def test_open_mode_legs_reports_signed_units(
        self, store: TradingStore, clock: FrozenClock
    ) -> None:
        runner = _open_long(store, clock)
        position = runner.trade_manager.list_positions()[0]
        intent, _decision = runner.open_book[position.trade_id]
        legs = runner.open_mode_legs()
        assert legs == (
            (
                position.legs[0].contract.symbol,
                intent.mode_id,
                position.legs[0].quantity_contracts,
            ),
        )

    def test_only_invalidated_theses_of_the_mode_are_exited(
        self, store: TradingStore, clock: FrozenClock
    ) -> None:
        """Decay exits are scoped to one mode and to intents the predicate rejects."""
        runner = _open_long(store, clock)
        position = runner.trade_manager.list_positions()[0]
        intent, _decision = runner.open_book[position.trade_id]
        assert intent.mode_id is not None
        symbol = position.legs[0].contract.symbol
        snapshots = {symbol: _exit_quote(position.legs[0].contract, clock)}
        other = next(mode for mode in ModeId if mode is not intent.mode_id)
        assert not runner.exit_invalidated_theses(
            other, invalidated=lambda _i: True, detail="x", snapshots=snapshots
        )
        assert not runner.exit_invalidated_theses(
            intent.mode_id,
            invalidated=lambda _i: False,
            detail="x",
            snapshots=snapshots,
        )
        still_open = runner.trade_manager.get_position(position.trade_id)
        assert still_open is not None and still_open.state is TradeState.OPEN

        events = runner.exit_invalidated_theses(
            intent.mode_id,
            invalidated=lambda _i: True,
            detail="order flow reversed",
            snapshots=snapshots,
        )
        assert events
        closed = runner.trade_manager.get_position(position.trade_id)
        assert closed is not None and closed.state is TradeState.CLOSED
