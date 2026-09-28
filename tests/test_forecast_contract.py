"""ModeForecast contract, ledger persistence and thesis-exit plumbing."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

import tests.factories as f
from trading.domain.clock import FrozenClock
from trading.domain.contracts import DerivativesContext
from trading.domain.contracts.forecast import (
    ForecastBarrier,
    ForecastEventKind,
    ForecastLabel,
    ForecastLeg,
    ModeForecast,
)
from trading.domain.enums import ModeId, OptionType, ReasonCode, Side
from trading.runtime.forecast_stage import intent_direction
from trading.runtime.paper_runner import PaperStrategyRequest, _thesis_exit_template
from trading.storage.trading_store import TradingEventType, TradingStore
from trading.strategies._common import resolve_direction
from trading.strategies.macro import MacroBias
from trading.trade.exits import ExitEngine, ExitKind

NOW = datetime(2026, 9, 14, 4, 0, tzinfo=UTC)


def _forecast(**overrides: Any) -> ModeForecast:
    return ModeForecast.model_validate(
        {
            "forecast_id": "FC-1",
            "cycle_id": "FCY-1",
            "as_of": NOW,
            "mode_id": ModeId.M2_DIRECTIONAL,
            "model_version": "forecast-v1",
            "config_version": "forecast-v1+abc",
            "horizon_seconds": 14_400,
            "spot": Decimal("24000"),
            "direction": 1,
            "p_up": Decimal("0.62"),
            "event": ForecastEventKind.UP_MOVE,
            "event_threshold": Decimal("0.004"),
            "p_event": Decimal("0.31"),
            "forecast_drift": Decimal("0.0021"),
            "forecast_sigma": Decimal("0.0068"),
            "target_move_fraction": Decimal("0.0068"),
            "stop_move_fraction": Decimal("0.0034"),
            "gate_passed": True,
            "gate_enforced": False,
            **overrides,
        }
    )


def _leg() -> ForecastLeg:
    return ForecastLeg(
        symbol="NIFTY26SEP24000CE",
        option_type=OptionType.CALL,
        strike=Decimal("24000"),
        side=Side.BUY,
        ratio=1,
        expiry_years=Decimal("0.02"),
        mid=Decimal("120"),
        implied_volatility=Decimal("0.14"),
    )


@pytest.fixture
def store(tmp_path: Path) -> Iterator[TradingStore]:
    trading_store = TradingStore.open(
        tmp_path / "trading.sqlite", clock=FrozenClock(NOW)
    )
    yield trading_store
    trading_store.close()


class TestModeForecastContract:
    def test_priced_structure_requires_cost_value_and_edge(self) -> None:
        """A structure forecast without its edge cannot be evaluated later."""
        with pytest.raises(ValidationError):
            _forecast(legs=(_leg(),), structure_cost=Decimal("120"))

    def test_pricing_without_legs_is_rejected(self) -> None:
        """An edge with no structure behind it is meaningless."""
        with pytest.raises(ValidationError):
            _forecast(edge_after_costs=Decimal("3"))

    def test_probabilities_are_bounded(self) -> None:
        """Calibration math assumes p_up and p_event lie in [0, 1]."""
        with pytest.raises(ValidationError):
            _forecast(p_up=Decimal("1.2"))

    def test_json_round_trip_is_lossless(self) -> None:
        """Forecasts replay bit-for-bit from the ledger (Decimal, not float)."""
        forecast = _forecast(
            family_id="bull_call_debit",
            legs=(_leg(),),
            structure_cost=Decimal("120"),
            forecast_value=Decimal("131.5"),
            implied_value=Decimal("119.2"),
            edge_after_costs=Decimal("4.25"),
            p_profit=Decimal("0.47"),
            reason_codes=(ReasonCode.FORECAST_VIEW_MISMATCH,),
            features={"gap_z": Decimal("0.8")},
            absent_features=("futures_basis",),
        )
        restored = ModeForecast.model_validate_json(forecast.model_dump_json())
        assert restored == forecast
        assert isinstance(restored.edge_after_costs, Decimal)


class TestForecastLedger:
    def test_forecast_and_label_rehydrate_from_store(self, store: TradingStore) -> None:
        """Rejected and traded forecasts share one durable, typed ledger."""
        forecast = _forecast(reason_codes=(ReasonCode.FORECAST_EDGE_INSUFFICIENT,))
        label = ForecastLabel(
            label_id="FL-1",
            forecast_id="FC-1",
            mode_id=ModeId.M2_DIRECTIONAL,
            labelled_at=NOW + timedelta(hours=5),
            horizon_end=NOW + timedelta(hours=4),
            complete=True,
            barrier=ForecastBarrier.TARGET,
            barrier_at=NOW + timedelta(hours=1),
            event_occurred=True,
            realized_return=Decimal("0.007"),
            max_favorable=Decimal("0.009"),
            max_adverse=Decimal("-0.001"),
            bars_used=48,
        )
        store.append(TradingEventType.MODE_FORECAST, forecast, event_id="FC-1")
        store.append(TradingEventType.FORECAST_LABEL, label, event_id="FL-1")
        events = store.read_events()
        assert [event.deserialize() for event in events] == [forecast, label]


class TestThesisExitTemplate:
    def _request(self, **overrides: Any) -> PaperStrategyRequest:
        return PaperStrategyRequest(
            strategy_id="debit_spread",
            underlying=f.snapshot(),
            candidates=(),
            instruments={},
            event_risk_state=None,
            experiment_id="EXP-1",
            **overrides,
        )

    def test_time_exit_only_tightens(self) -> None:
        """A forecast horizon may shorten a hold, never extend a stricter one."""
        early = NOW + timedelta(hours=1)
        late = NOW + timedelta(hours=6)
        template = f.exit_template(time_exit=early)
        updated = _thesis_exit_template(template, self._request(thesis_time_exit=late))
        assert updated.time_exit == early
        tighter = _thesis_exit_template(
            f.exit_template(time_exit=late), self._request(thesis_time_exit=early)
        )
        assert tighter.time_exit == early

    def test_underlying_levels_are_attached(self) -> None:
        """The mode's invalidation level travels with the intent to the exit engine."""
        updated = _thesis_exit_template(
            f.exit_template(),
            self._request(underlying_stop_below=Decimal("23850")),
        )
        assert updated.underlying_stop_below == Decimal("23850")
        assert updated.underlying_stop_above is None

    def test_no_overlay_leaves_template_untouched(self) -> None:
        """Default-off forecasting changes nothing about existing protection."""
        template = f.exit_template()
        assert _thesis_exit_template(template, self._request()) is template


class TestUnderlyingInvalidationExit:
    def _feature(self, spot: str) -> Any:
        return f.snapshot(
            contract=f.option_contract(),
            derivatives=DerivativesContext(
                days_to_expiry=5,
                option_type=OptionType.CALL,
                underlying_price=f.price(spot),
            ),
        )

    def test_breach_below_forces_stop(self) -> None:
        """Invariant: a broken thesis level exits even when option stops hold."""
        intent = f.intent(
            exit_template=f.exit_template(underlying_stop_below=Decimal("23900"))
        )
        evaluation = ExitEngine().evaluate(
            f.position_state(), self._feature("23890"), intent, now=NOW
        )
        assert evaluation.kind is ExitKind.STOP
        assert "underlying invalidation" in evaluation.detail

    def test_level_intact_defers_to_price_rules(self) -> None:
        """No breach: the ordinary leg-price stop/target logic decides."""
        intent = f.intent(
            exit_template=f.exit_template(underlying_stop_below=Decimal("23900"))
        )
        evaluation = ExitEngine().evaluate(
            f.position_state(), self._feature("24010"), intent, now=NOW
        )
        assert "underlying invalidation" not in evaluation.detail

    def test_missing_underlying_price_never_invents_a_breach(self) -> None:
        """Unknown spot does not trigger a thesis exit on its own."""
        intent = f.intent(
            exit_template=f.exit_template(underlying_stop_above=Decimal("24100"))
        )
        feature = f.snapshot(
            contract=f.option_contract(),
            derivatives=DerivativesContext(days_to_expiry=5),
        )
        evaluation = ExitEngine().evaluate(f.position_state(), feature, intent, now=NOW)
        assert "underlying invalidation" not in evaluation.detail


class TestSingleDirectionSource:
    def test_mode_direction_overrides_technical_read(self) -> None:
        """A confirmed bearish family is never flipped by a bullish tick."""
        underlying = f.snapshot(
            market=f.quote(last=f.price("101"), close=f.price("100"))
        )
        bias, _ = resolve_direction(underlying, None, NOW, mode_direction=-1)
        assert bias is MacroBias.BEARISH
        legacy, _ = resolve_direction(underlying, None, NOW)
        assert legacy is MacroBias.BULLISH

    def test_intent_direction_reads_structure(self) -> None:
        """Long call is +1; a short call alone is -1."""
        long_call = f.intent()
        assert intent_direction(long_call) == 1
        short_call = f.intent(legs=(f.leg(side=Side.SELL),))
        assert intent_direction(short_call) == -1
