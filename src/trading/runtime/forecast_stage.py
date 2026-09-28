"""Per-cycle forecast stage between family binding and request assembly.

For every mode it builds that mode's own view, prices each bound structure
under the forecast and the implied distribution, and records a ``ModeForecast``.
With ``enforce`` off (the default) nothing about trading changes; with it on a
failed gate turns the family into a SHADOW evaluation with the gate's reasons.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from trading.config.forecast import ForecastConfig, ModeForecastConfig
from trading.domain.contracts import (
    FeatureSnapshot,
    InstrumentSpec,
    MarketState,
    TradeIntent,
)
from trading.domain.contracts.forecast import ForecastLeg, ModeForecast
from trading.domain.enums import ExecutionMode, FamilyId, ModeId, OptionType, ReasonCode
from trading.forecast.bars import Bar
from trading.forecast.common import event_probability
from trading.forecast.distribution import (
    HorizonDistribution,
    implied_distribution,
    price_structure,
    structure_cost,
)
from trading.forecast.inputs import ForecastInputs, ModeView, PositioningStats
from trading.forecast.m1 import m1_cost_hurdle, m1_view
from trading.forecast.m2 import m2_view
from trading.forecast.m3 import m3_view
from trading.forecast.m4 import WeeklyRegime, m4_regime, m4_regime_is_stale, m4_view
from trading.forecast.order_flow import OrderFlowTracker, Quote
from trading.forecast.reference import ReferenceInputs
from trading.forecast.structures import (
    FAMILY_DIRECTION,
    legs_for_family,
    max_loss_per_unit,
    structure_delta,
    structure_friction,
    structure_invalidation,
    suggest_vertical,
)
from trading.identification.binders import BoundCandidates
from trading.identification.p1_features import ObservedP1Features
from trading.runtime.session_routing import ProducedFamilyRequest
from trading.strategies.macro import MacroAssessment, MacroBias, accepted_macro_bias

__all__ = [
    "ExitOverlay",
    "ForecastCycleInputs",
    "ForecastStage",
    "ForecastStageResult",
    "OpenLeg",
    "intent_direction",
]

_ZERO = Decimal(0)
_ONE = Decimal(1)
_TWO = Decimal(2)
_HUNDRED = Decimal(100)
_QUANT = Decimal("0.000001")
_FALLBACK_VOL = Decimal("0.15")
_MACRO_MIN_CONFIDENCE = Decimal("0.6")
_FLATTEN_IST = time(15, 20)
_CALENDAR_PER_TRADING = Decimal(7) / Decimal(5)
_STRUCTURED_MODES = (ModeId.M3_TACTICAL_POSITIONAL,)


def intent_direction(intent: TradeIntent) -> int:
    """Net directional sign of an option intent: long calls or short puts are +1."""
    score = 0
    for leg in intent.legs:
        option_type = leg.contract.option_type
        if option_type is None:
            continue
        flavour = 1 if option_type is OptionType.CALL else -1
        score += leg.signed_ratio * flavour
    return (score > 0) - (score < 0)


@dataclass(frozen=True, slots=True)
class OpenLeg:
    """One open position leg, signed in contract units (+ long, - short)."""

    symbol: str
    mode_id: ModeId
    signed_units: Decimal


@dataclass(frozen=True, slots=True)
class ExitOverlay:
    """Forecast-derived protection attached to a family's new intents."""

    underlying_stop_below: Decimal | None = None
    underlying_stop_above: Decimal | None = None
    time_exit: datetime | None = None


@dataclass(frozen=True, slots=True)
class ForecastCycleInputs:
    """Everything the builder hands the stage for one cycle."""

    cycle_id: str
    as_of: datetime
    zone: ZoneInfo
    underlying: FeatureSnapshot
    bars: tuple[Bar, ...]
    market: MarketState | None
    p1: ObservedP1Features | None
    chain: tuple[FeatureSnapshot, ...]
    instruments: Mapping[str, InstrumentSpec]
    charges_per_lot_leg: Decimal
    max_spread_fraction: Decimal
    min_open_interest: int
    vix_history: tuple[Decimal, ...] = ()
    macro: MacroAssessment | None = None
    reference: ReferenceInputs = field(default_factory=ReferenceInputs)
    open_legs: tuple[OpenLeg, ...] = ()


@dataclass(frozen=True, slots=True)
class ForecastStageResult:
    """Adjusted producer rows plus everything to persist and attach."""

    produced: tuple[ProducedFamilyRequest, ...]
    forecasts: tuple[ModeForecast, ...]
    overlays: dict[tuple[ModeId, FamilyId], ExitOverlay]
    directions: dict[tuple[ModeId, FamilyId], int]
    views: dict[ModeId, ModeView]


@dataclass(frozen=True, slots=True)
class _SessionPositioning:
    session: date
    pcr: Decimal
    call_wall: Decimal
    put_wall: Decimal


def _mode_config(config: ForecastConfig, mode_id: ModeId) -> ModeForecastConfig:
    return {
        ModeId.M1_CAS: config.m1,
        ModeId.M2_DIRECTIONAL: config.m2,
        ModeId.M3_TACTICAL_POSITIONAL: config.m3,
        ModeId.M4_STRATEGIC_POSITIONAL: config.m4,
    }[mode_id]


def _q(value: Decimal) -> Decimal:
    return value.quantize(_QUANT)


def _q_prob(value: Decimal) -> Decimal:
    return min(max(_q(value), _ZERO), _ONE)


class ForecastStage:
    """Stateful across cycles only for rolling order flow, session OI and M4's week."""

    def __init__(
        self,
        config: ForecastConfig,
        *,
        config_version: str,
        new_id: Callable[[str], str],
    ) -> None:
        self._config = config
        self._version = config_version
        self._new_id = new_id
        self._flow = OrderFlowTracker(
            short_seconds=config.m1.ofi_short_seconds,
            long_seconds=config.m1.ofi_long_seconds,
        )
        self._positioning: _SessionPositioning | None = None
        self._regime: WeeklyRegime | None = None
        self._last_recorded: dict[tuple[ModeId, str], datetime] = {}
        self._last_views: dict[ModeId, ModeView] = {}

    @property
    def config(self) -> ForecastConfig:
        return self._config

    @property
    def weekly_regime(self) -> WeeklyRegime | None:
        return self._regime

    def enforce_exits(self, mode_id: ModeId) -> bool:
        return _mode_config(self._config, mode_id).enforce_exits

    # ----------------------------------------------------------------- inputs

    def observe_quote(self, role: str, snapshot: FeatureSnapshot) -> None:
        """Feed an at-the-money CALL or PUT quote into the order-flow tracker."""
        market = snapshot.market
        if (
            market.bid is None
            or market.ask is None
            or market.bid_size is None
            or market.ask_size is None
        ):
            return
        self._flow.observe(
            role,
            Quote(
                at=snapshot.times.event_time,
                bid=market.bid.value,
                bid_size=Decimal(market.bid_size),
                ask=market.ask.value,
                ask_size=Decimal(market.ask_size),
            ),
        )

    def _observe_chain(self, chain: Sequence[FeatureSnapshot], spot: Decimal) -> None:
        near = _nearest_expiry(chain)
        for option_type, role in ((OptionType.CALL, "CALL"), (OptionType.PUT, "PUT")):
            pool = [
                item
                for item in near
                if item.contract.option_type is option_type and item.contract.strike
            ]
            if pool:
                atm = min(
                    pool, key=lambda item: abs((item.contract.strike or _ZERO) - spot)
                )
                self.observe_quote(role, atm)

    def _positioning_stats(
        self, chain: Sequence[FeatureSnapshot], spot: Decimal, session: date
    ) -> PositioningStats | None:
        near = _nearest_expiry(chain)
        calls = [
            (item.contract.strike, item.derivatives.open_interest)
            for item in near
            if item.contract.option_type is OptionType.CALL
            and item.contract.strike is not None
            and item.derivatives is not None
            and item.derivatives.open_interest
        ]
        puts = [
            (item.contract.strike, item.derivatives.open_interest)
            for item in near
            if item.contract.option_type is OptionType.PUT
            and item.contract.strike is not None
            and item.derivatives is not None
            and item.derivatives.open_interest
        ]
        call_oi = sum((Decimal(oi) for _k, oi in calls if oi), _ZERO)
        put_oi = sum((Decimal(oi) for _k, oi in puts if oi), _ZERO)
        if call_oi <= 0 or put_oi <= 0:
            return None
        pcr = put_oi / call_oi
        call_wall = max(calls, key=lambda row: row[1] or 0)[0] or _ZERO
        put_wall = max(puts, key=lambda row: row[1] or 0)[0] or _ZERO
        baseline = self._positioning
        if baseline is None or baseline.session != session:
            baseline = _SessionPositioning(session, pcr, call_wall, put_wall)
            self._positioning = baseline
        shift = (
            ((call_wall - baseline.call_wall) + (put_wall - baseline.put_wall))
            / _TWO
            / (spot / _HUNDRED)
        )
        return PositioningStats(
            pcr=_q(pcr), pcr_change=_q(pcr - baseline.pcr), oi_wall_shift=_q(shift)
        )

    def _inputs(self, ctx: ForecastCycleInputs, spot: Decimal) -> ForecastInputs:
        session = ctx.as_of.astimezone(ctx.zone).date()
        self._observe_chain(ctx.chain, spot)
        macro_bias = None
        accepted = accepted_macro_bias(
            ctx.macro, now=ctx.as_of, min_confidence=_MACRO_MIN_CONFIDENCE
        )
        if accepted is not None and ctx.macro is not None:
            sign = _ONE if accepted is MacroBias.BULLISH else -_ONE
            macro_bias = sign * ctx.macro.confidence
        current_vix = ctx.vix_history[-1] if ctx.vix_history else None
        if current_vix is None:
            current_vix = ctx.underlying.features.get("india_vix")
        previous_vix = ctx.vix_history[-2] if len(ctx.vix_history) >= 2 else None  # noqa: PLR2004
        return ForecastInputs(
            as_of=ctx.as_of,
            zone=ctx.zone,
            spot=spot,
            bars=ctx.bars,
            market=ctx.market,
            current_vix=current_vix,
            previous_vix=previous_vix,
            iv_percentile=None if ctx.market is None else ctx.market.iv_percentile,
            atm_iv_by_expiry=() if ctx.p1 is None else ctx.p1.atm_iv_by_expiry,
            reference=ctx.reference,
            positioning=self._positioning_stats(ctx.chain, spot, session),
            order_flow=self._flow.stats(ctx.as_of),
            macro_bias=macro_bias,
            underlying_features=dict(ctx.underlying.features),
            expiry_today=any(
                item.derivatives is not None and item.derivatives.days_to_expiry == 0
                for item in ctx.chain
            ),
        )

    # ------------------------------------------------------------------ views

    def _views(self, inputs: ForecastInputs) -> dict[ModeId, ModeView]:
        config = self._config
        regime = self._regime
        if regime is None or m4_regime_is_stale(regime, inputs, config):
            regime = m4_regime(inputs, config)
            self._regime = regime
        views = {
            ModeId.M1_CAS: m1_view(inputs, config),
            ModeId.M2_DIRECTIONAL: m2_view(inputs, config),
            ModeId.M3_TACTICAL_POSITIONAL: m3_view(inputs, config),
            ModeId.M4_STRATEGIC_POSITIONAL: m4_view(regime, config),
        }
        self._last_views = views
        return views

    def m1_signal_decayed(self, direction: int) -> bool:
        """True when the latest M1 view no longer supports an open ``direction``."""
        view = self._last_views.get(ModeId.M1_CAS)
        if view is None or direction == 0:
            return False
        support = view.p_up if direction > 0 else _ONE - view.p_up
        return support < self._config.m1.decay_exit_probability

    # ------------------------------------------------------------------- main

    def evaluate(
        self,
        produced: Sequence[ProducedFamilyRequest],
        ctx: ForecastCycleInputs,
    ) -> ForecastStageResult:
        """Forecast every mode, gate each family, and collect records to persist."""
        last = ctx.underlying.market.last
        if last is None or last.value <= 0:
            return ForecastStageResult(tuple(produced), (), {}, {}, {})
        spot = last.value
        inputs = self._inputs(ctx, spot)
        views = self._views(inputs)
        forecasts: list[ModeForecast] = [
            self._record(ctx=ctx, view=view, spot=spot)
            for mode_id, view in views.items()
            if self._due(mode_id, "", ctx.as_of)
        ]
        adjusted: list[ProducedFamilyRequest] = []
        overlays: dict[tuple[ModeId, FamilyId], ExitOverlay] = {}
        directions: dict[tuple[ModeId, FamilyId], int] = {}
        for item in produced:
            mode_id, family = item.spec.mode_id, item.spec.family_id
            view = views[mode_id]
            mode_cfg = _mode_config(self._config, mode_id)
            if mode_cfg.enforce and FAMILY_DIRECTION.get(family, 0):
                directions[(mode_id, family)] = FAMILY_DIRECTION[family]
            bound = item.bound
            if mode_cfg.enforce and mode_id in _STRUCTURED_MODES and bound.candidates:
                bound = self._forecast_strikes(family, bound, view, ctx, spot)
            priced, legs = self._price_and_gate(item, bound, view, ctx, spot)
            if item.execute or self._due(mode_id, family.value, ctx.as_of):
                forecasts.append(priced)
            if mode_cfg.enforce_exits:
                overlays[(mode_id, family)] = self._overlay(family, legs, view, ctx)
            if mode_cfg.enforce and priced.reason_codes and item.execute:
                adjusted.append(_shadowed(item, bound, priced.reason_codes))
            elif bound is not item.bound:
                adjusted.append(replace(item, bound=bound))
            else:
                adjusted.append(item)
        return ForecastStageResult(
            produced=tuple(adjusted),
            forecasts=tuple(forecasts),
            overlays=overlays,
            directions=directions,
            views=views,
        )

    def _price_and_gate(
        self,
        item: ProducedFamilyRequest,
        bound: BoundCandidates,
        view: ModeView,
        ctx: ForecastCycleInputs,
        spot: Decimal,
    ) -> tuple[ModeForecast, tuple[ForecastLeg, ...]]:
        """Price the bound structure and attach every failed gate."""
        family = item.spec.family_id
        mode_cfg = _mode_config(self._config, item.spec.mode_id)
        reasons = self._gate(
            item=item, bound=bound, view=view, ctx=ctx, spot=spot, mode_cfg=mode_cfg
        )
        legs = legs_for_family(family, bound.candidates) or ()
        priced = self._record(
            ctx=ctx,
            view=view,
            spot=spot,
            family=family,
            candidates=bound.candidates,
            legs=legs,
            reasons=reasons,
        )
        edge_reason = self._edge_reason(priced, mode_cfg) if priced.legs else None
        if edge_reason is not None:
            priced = priced.model_copy(
                update={
                    "reason_codes": tuple(
                        dict.fromkeys((*priced.reason_codes, edge_reason))
                    ),
                    "gate_passed": False,
                }
            )
        return priced, legs

    def _due(self, mode_id: ModeId, key: str, now: datetime) -> bool:
        interval = _mode_config(self._config, mode_id).record_interval_seconds
        previous = self._last_recorded.get((mode_id, key))
        if previous is not None and (now - previous).total_seconds() < interval:
            return False
        self._last_recorded[(mode_id, key)] = now
        return True

    def _gate(
        self,
        *,
        item: ProducedFamilyRequest,
        bound: BoundCandidates,
        view: ModeView,
        ctx: ForecastCycleInputs,
        spot: Decimal,
        mode_cfg: ModeForecastConfig,
    ) -> tuple[ReasonCode, ...]:
        """Every forecast reason this family fails, before pricing."""
        mode_id, family = item.spec.mode_id, item.spec.family_id
        reasons: list[ReasonCode] = list(view.reason_codes)
        local = ctx.as_of.astimezone(ctx.zone).time()
        if mode_cfg.entry_windows_ist and not any(
            window.contains(local) for window in mode_cfg.entry_windows_ist
        ):
            reasons.append(ReasonCode.FORECAST_OUTSIDE_ENTRY_WINDOW)
        family_direction = FAMILY_DIRECTION.get(family, 0)
        if family_direction and family_direction != view.direction:
            reasons.append(ReasonCode.FORECAST_DIRECTION_CONFLICT)
        if view.allowed_families is not None and family not in view.allowed_families:
            reasons.append(ReasonCode.FORECAST_VIEW_MISMATCH)
        if mode_id is ModeId.M1_CAS and bound.candidates:
            reason = self._m1_hurdle(bound.candidates[0], view, ctx, spot)
            if reason is not None:
                reasons.append(reason)
        if mode_id is ModeId.M4_STRATEGIC_POSITIONAL and bound.candidates:
            reason = self._basket_reason(family, bound.candidates, ctx)
            if reason is not None:
                reasons.append(reason)
        return tuple(dict.fromkeys(reasons))

    def _lot_size(self, ctx: ForecastCycleInputs, symbol: str) -> Decimal:
        spec = ctx.instruments.get(symbol)
        return (
            Decimal(spec.lot_size) if spec is not None and spec.lot_size > 0 else _ONE
        )

    def _m1_hurdle(
        self,
        candidate: FeatureSnapshot,
        view: ModeView,
        ctx: ForecastCycleInputs,
        spot: Decimal,
    ) -> ReasonCode | None:
        greeks = None if candidate.derivatives is None else candidate.derivatives.greeks
        bid, ask = candidate.market.bid, candidate.market.ask
        if bid is None or ask is None:
            return ReasonCode.FORECAST_INPUT_ABSENT
        ok, _expected = m1_cost_hurdle(
            spot=spot,
            sigma=view.distribution.sigma,
            delta=None if greeks is None else greeks.delta,
            gamma=None if greeks is None else greeks.gamma,
            spread=ask.value - bid.value,
            cost_per_unit=ctx.charges_per_lot_leg
            / self._lot_size(ctx, candidate.contract.symbol),
            multiple=self._config.m1.cost_hurdle_multiple,
        )
        return None if ok else ReasonCode.FORECAST_COST_HURDLE

    def _basket_reason(
        self,
        family: FamilyId,
        candidates: Sequence[FeatureSnapshot],
        ctx: ForecastCycleInputs,
    ) -> ReasonCode | None:
        """Refuse a structure that pushes M4 net delta further past its budget."""
        legs = legs_for_family(family, candidates)
        if not legs:
            return None
        per_unit = structure_delta(legs, candidates)
        if per_unit is None:
            return None
        lot = self._lot_size(ctx, legs[0].symbol)
        by_symbol = {item.contract.symbol: item for item in ctx.chain}
        current = _ZERO
        for leg in ctx.open_legs:
            if leg.mode_id is not ModeId.M4_STRATEGIC_POSITIONAL:
                continue
            snapshot = by_symbol.get(leg.symbol)
            greeks = (
                None
                if snapshot is None or snapshot.derivatives is None
                else snapshot.derivatives.greeks
            )
            if greeks is not None and greeks.delta is not None:
                current += leg.signed_units * greeks.delta
        after = current + per_unit * lot
        limit = self._config.m4.max_net_delta_units
        if abs(after) > limit and abs(after) > abs(current):
            return ReasonCode.FORECAST_BASKET_LIMIT
        return None

    def _edge_reason(
        self, forecast: ModeForecast, mode_cfg: ModeForecastConfig
    ) -> ReasonCode | None:
        if forecast.edge_after_costs is None or forecast.structure_cost is None:
            return None
        risk = max_loss_per_unit(forecast.legs, forecast.structure_cost)
        floor = mode_cfg.min_edge_fraction * (
            risk if risk > 0 else abs(forecast.structure_cost)
        )
        return (
            None
            if forecast.edge_after_costs >= floor
            else ReasonCode.FORECAST_EDGE_INSUFFICIENT
        )

    def _forecast_strikes(
        self,
        family: FamilyId,
        bound: BoundCandidates,
        view: ModeView,
        ctx: ForecastCycleInputs,
        spot: Decimal,
    ) -> BoundCandidates:
        """Replace the binder's vertical with strikes placed on the forecast."""
        first = bound.candidates[0]
        if first.derivatives is None or len(bound.candidates) != 2:  # noqa: PLR2004
            return bound
        mode = self._config.m3

        def tradable(item: FeatureSnapshot) -> bool:
            bid, ask = item.market.bid, item.market.ask
            oi = None if item.derivatives is None else item.derivatives.open_interest
            if bid is None or ask is None or bid.value <= 0 or oi is None:
                return False
            mid = (bid.value + ask.value) / _TWO
            spread_ok = (ask.value - bid.value) / mid <= ctx.max_spread_fraction
            return spread_ok and oi >= ctx.min_open_interest

        suggestion = suggest_vertical(
            family,
            ctx.chain,
            spot=spot,
            distribution=view.distribution,
            expiry_days=first.derivatives.days_to_expiry,
            credit_short_quantile=mode.credit_short_quantile,
            debit_target_quantile=mode.debit_target_quantile,
            credit_width_points=mode.credit_width_points,
            tradable=tradable,
        )
        if suggestion is None:
            return bound
        chosen = (suggestion.long_leg, suggestion.short_leg)
        binding = bound.binding.model_copy(
            update={"selected_symbols": tuple(item.contract.symbol for item in chosen)}
        )
        return replace(bound, candidates=chosen, binding=binding)

    def _overlay(
        self,
        family: FamilyId,
        legs: Sequence[ForecastLeg],
        view: ModeView,
        ctx: ForecastCycleInputs,
    ) -> ExitOverlay:
        """Underlying invalidation and, for multi-day modes, a thesis time exit."""
        below, above = structure_invalidation(
            family, legs, buffer_fraction=self._config.m4.short_strike_buffer_fraction
        )
        direction = FAMILY_DIRECTION.get(family, 0)
        if below is None and above is None:
            if direction > 0:
                below = view.invalidation_below
            elif direction < 0:
                above = view.invalidation_above
        time_exit = None
        if view.mode_id in {
            ModeId.M3_TACTICAL_POSITIONAL,
            ModeId.M4_STRATEGIC_POSITIONAL,
        }:
            mode = _mode_config(self._config, view.mode_id)
            days = Decimal(mode.horizon_minutes) / Decimal(self._config.session_minutes)
            calendar_days = int((days * _CALENDAR_PER_TRADING).to_integral_value())
            local = ctx.as_of.astimezone(ctx.zone) + timedelta(
                days=max(calendar_days, 1)
            )
            time_exit = local.replace(
                hour=_FLATTEN_IST.hour,
                minute=_FLATTEN_IST.minute,
                second=0,
                microsecond=0,
            ).astimezone(ctx.as_of.tzinfo)
        return ExitOverlay(
            underlying_stop_below=None if below is None else _q(below),
            underlying_stop_above=None if above is None else _q(above),
            time_exit=time_exit,
        )

    def _record(
        self,
        *,
        ctx: ForecastCycleInputs,
        view: ModeView,
        spot: Decimal,
        family: FamilyId | None = None,
        candidates: Sequence[FeatureSnapshot] = (),
        legs: Sequence[ForecastLeg] = (),
        reasons: Sequence[ReasonCode] = (),
    ) -> ModeForecast:
        mode_cfg = _mode_config(self._config, view.mode_id)
        implied: HorizonDistribution | None = None
        if view.implied_vol is not None and view.horizon_years > 0:
            implied = implied_distribution(view.implied_vol, view.horizon_years)
        update: dict[str, Any] = {}
        if legs and candidates:
            cost = structure_cost(legs)
            lot = self._lot_size(ctx, legs[0].symbol)
            friction = structure_friction(
                candidates, legs, charges_per_leg_unit=ctx.charges_per_lot_leg / lot
            )
            fallback = view.implied_vol or _FALLBACK_VOL
            forecast_price = price_structure(
                legs,
                spot=spot,
                distribution=view.distribution,
                horizon_years=view.horizon_years,
                cost=cost,
                cost_per_unit=friction,
                fallback_vol=fallback,
            )
            update = {
                "legs": tuple(legs),
                "structure_cost": _q(cost),
                "cost_per_unit": _q(friction),
                "forecast_value": _q(forecast_price.expected_value),
                "edge_after_costs": _q(forecast_price.expected_value - cost - friction),
                "p_profit": _q_prob(forecast_price.p_profit),
            }
            if implied is not None:
                implied_price = price_structure(
                    legs,
                    spot=spot,
                    distribution=implied,
                    horizon_years=view.horizon_years,
                    cost=cost,
                    cost_per_unit=friction,
                    fallback_vol=fallback,
                )
                update["implied_value"] = _q(implied_price.expected_value)
                update["p_profit_implied"] = _q_prob(implied_price.p_profit)
        all_reasons = tuple(dict.fromkeys((*view.reason_codes, *reasons)))
        return ModeForecast(
            forecast_id=self._new_id("FCST"),
            cycle_id=ctx.cycle_id,
            as_of=ctx.as_of,
            mode_id=view.mode_id,
            family_id=None if family is None else family.value,
            model_version=self._config.model_version,
            config_version=self._version,
            horizon_seconds=mode_cfg.horizon_minutes * 60,
            spot=spot,
            direction=view.direction,
            p_up=_q_prob(view.p_up),
            event=view.event,
            event_threshold=_q(view.event_threshold),
            p_event=_q_prob(
                event_probability(view.distribution, view.event, view.event_threshold)
            ),
            p_event_implied=(
                None
                if implied is None
                else _q_prob(
                    event_probability(implied, view.event, view.event_threshold)
                )
            ),
            forecast_drift=_q(view.distribution.drift),
            forecast_sigma=_q(view.distribution.sigma),
            implied_sigma=None if implied is None else _q(implied.sigma),
            target_move_fraction=_q(view.target_move_fraction),
            stop_move_fraction=_q(view.stop_move_fraction),
            invalidation_below=(
                None
                if view.invalidation_below is None or view.invalidation_below <= 0
                else _q(view.invalidation_below)
            ),
            invalidation_above=(
                None
                if view.invalidation_above is None or view.invalidation_above <= 0
                else _q(view.invalidation_above)
            ),
            view=view.view,
            gate_passed=not all_reasons,
            gate_enforced=mode_cfg.enforce,
            reason_codes=all_reasons,
            features={name: _q(value) for name, value in view.features.items()},
            absent_features=view.absent,
            **update,
        )


def _nearest_expiry(chain: Sequence[FeatureSnapshot]) -> list[FeatureSnapshot]:
    dated = [
        item
        for item in chain
        if item.derivatives is not None and item.contract.expiry is not None
    ]
    if not dated:
        return []
    nearest = min(item.contract.expiry for item in dated if item.contract.expiry)
    return [item for item in dated if item.contract.expiry == nearest]


def _shadowed(
    item: ProducedFamilyRequest,
    bound: BoundCandidates,
    reasons: Sequence[ReasonCode],
) -> ProducedFamilyRequest:
    """Keep the binding visible for the record, but stop execution."""
    binding = bound.binding.model_copy(
        update={
            "reason_codes": tuple(
                dict.fromkeys((*bound.binding.reason_codes, *reasons))
            )
        }
    )
    return replace(
        item,
        bound=replace(bound, binding=binding),
        execute=False,
        execution_mode=ExecutionMode.SHADOW,
    )
