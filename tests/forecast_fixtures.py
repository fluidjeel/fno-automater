"""Deterministic synthetic NIFTY sessions and option chains for forecast tests."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import tests.factories as f
from trading.config.forecast import ForecastConfig, load_forecast_config
from trading.domain.contracts import DerivativesContext, FeatureSnapshot, Greeks
from trading.domain.contracts.identification import CandidateBinding
from trading.domain.enums import ExecutionMode, FamilyId, ModeId, OptionType
from trading.forecast.bars import Bar
from trading.forecast.distribution import black_price
from trading.forecast.normal import norm_cdf
from trading.identification.binders import BoundCandidates
from trading.runtime.forecast_stage import ForecastCycleInputs
from trading.runtime.session_routing import FamilyProducerSpec, ProducedFamilyRequest

IST = ZoneInfo("Asia/Kolkata")
REPO = Path(__file__).resolve().parents[1]
BARS_PER_SESSION = 75


def forecast_config(**mode_updates: dict[str, object]) -> ForecastConfig:
    """Repo forecast config with optional per-mode field overrides."""
    config, _version = load_forecast_config(REPO / "config" / "forecast.yaml")
    updates = {
        name: getattr(config, name).model_copy(update=values)
        for name, values in mode_updates.items()
    }
    return config.model_copy(update=updates) if updates else config


def ist(day: date, hour: int, minute: int) -> datetime:
    return datetime.combine(day, time(hour, minute), IST).astimezone(UTC)


def weekdays_before(end: date, count: int) -> list[date]:
    """``count`` weekdays strictly before ``end``, oldest first."""
    days: list[date] = []
    cursor = end - timedelta(days=1)
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    return list(reversed(days))


def _triangle(index: int, period: int) -> Decimal:
    half = period // 2
    return Decimal(abs((index % period) - half) - half // 2)


def price_path(
    count: int,
    *,
    start: Decimal,
    slope: Decimal,
    wave: Decimal = Decimal(0),
    period: int = 36,
    noise: Decimal = Decimal(2),
) -> list[Decimal]:
    """Trend plus a triangle swing plus alternating tick noise.

    Noise alternates within a session so every session closes on the same
    side, keeping daily closes free of artificial parity swings.
    """
    return [
        start
        + slope * index
        + wave * _triangle(index, period)
        + (noise if (index % BARS_PER_SESSION) % 2 else -noise)
        for index in range(count)
    ]


def session_bars(
    days: list[date], closes: list[Decimal], *, wick: Decimal = Decimal(3)
) -> tuple[Bar, ...]:
    """Lay ``closes`` onto consecutive 5-minute session bars across ``days``."""
    bars: list[Bar] = []
    previous = closes[0]
    index = 0
    for day in days:
        open_at = ist(day, 9, 15)
        for slot in range(BARS_PER_SESSION):
            if index >= len(closes):
                return tuple(bars)
            close = closes[index]
            bars.append(
                Bar(
                    start=open_at + timedelta(minutes=5 * slot),
                    open=previous,
                    high=max(previous, close) + wick,
                    low=min(previous, close) - wick,
                    close=close,
                    volume=Decimal(1000),
                )
            )
            previous = close
            index += 1
    return tuple(bars)


def _tick(value: Decimal) -> Decimal:
    return (value / Decimal("0.05")).to_integral_value() * Decimal("0.05")


def option(
    strike: int,
    option_type: OptionType,
    *,
    spot: Decimal,
    dte: int,
    expiry: date,
    iv: Decimal = Decimal("0.14"),
    half_spread: Decimal = Decimal("0.5"),
    open_interest: int = 50_000,
    at: datetime | None = None,
) -> FeatureSnapshot:
    """Option snapshot priced with Black-Scholes so mids and deltas agree."""
    years = Decimal(dte) / Decimal(365)
    mid = black_price(option_type, spot, Decimal(strike), iv, years)
    mid = max(mid, Decimal("1"))
    d1 = ((spot / Decimal(strike)).ln() + iv * iv / 2 * years) / (iv * years.sqrt())
    delta = norm_cdf(d1) if option_type is OptionType.CALL else norm_cdf(d1) - 1
    suffix = "CE" if option_type is OptionType.CALL else "PE"
    symbol = f"NIFTY{expiry:%y%b}{strike}{suffix}".upper()
    stamp = at or f.NOW
    return f.snapshot(
        snapshot_id=f"SNAP-{symbol}",
        contract=f.option_contract(
            symbol=symbol,
            strike=Decimal(strike),
            option_type=option_type,
            expiry=expiry,
        ),
        times=f.snapshot_times(
            event_time=stamp,
            source_time=stamp,
            receive_time=stamp,
            calculation_time=stamp,
        ),
        market=f.quote(
            bid=f.price(str(_tick(mid - half_spread))),
            ask=f.price(str(_tick(mid + half_spread))),
            last=f.price(str(_tick(mid))),
            bid_size=1500,
            ask_size=1500,
        ),
        derivatives=DerivativesContext(
            days_to_expiry=dte,
            open_interest=open_interest,
            option_type=option_type,
            greeks=Greeks(
                model="bs",
                calculation_version="1",
                converged=True,
                implied_volatility=iv,
                delta=delta.quantize(Decimal("0.0001")),
                gamma=Decimal("0.0005"),
            ),
            underlying_price=f.price(str(spot)),
        ),
    )


def chain(
    *,
    spot: Decimal,
    centre: int,
    dte: int,
    expiry: date,
    width: int = 50,
    each_side: int = 10,
    iv: Decimal = Decimal("0.14"),
    at: datetime | None = None,
) -> tuple[FeatureSnapshot, ...]:
    return tuple(
        option(
            centre + width * step,
            kind,
            spot=spot,
            dte=dte,
            expiry=expiry,
            iv=iv,
            at=at,
        )
        for step in range(-each_side, each_side + 1)
        for kind in (OptionType.CALL, OptionType.PUT)
    )


def underlying(spot: Decimal, at: datetime) -> FeatureSnapshot:
    return f.snapshot(
        times=f.snapshot_times(
            event_time=at, source_time=at, receive_time=at, calculation_time=at
        ),
        market=f.quote(
            bid=f.price(str(spot - Decimal("0.5"))),
            ask=f.price(str(spot + Decimal("0.5"))),
            last=f.price(str(spot)),
        ),
    )


def produced(
    mode_id: ModeId,
    family: FamilyId,
    candidates: tuple[FeatureSnapshot, ...],
    *,
    execute: bool = True,
) -> ProducedFamilyRequest:
    binder: Callable[..., BoundCandidates] = lambda *_a, **_k: bound  # noqa: E731
    bound = BoundCandidates(
        binding=CandidateBinding(
            strategy_id="fixture",
            binding_version="1",
            selected_symbols=tuple(item.contract.symbol for item in candidates),
            score=Decimal("0.5"),
            eligible=True,
        ),
        candidates=candidates,
        setup_features=None,
    )
    return ProducedFamilyRequest(
        spec=FamilyProducerSpec(
            mode_id=mode_id,
            family_id=family,
            strategy_id="fixture",
            binder=binder,
        ),
        bound=bound,
        execute=execute,
        execution_mode=ExecutionMode.PAPER,
    )


def spot_of(ctx: ForecastCycleInputs) -> Decimal:
    last = ctx.underlying.market.last
    assert last is not None
    return last.value


def atm_strike(spot: Decimal) -> int:
    return int((spot / 50).to_integral_value()) * 50


def by_strike(
    items: tuple[FeatureSnapshot, ...], strike: int, option_type: OptionType
) -> FeatureSnapshot:
    return next(
        item
        for item in items
        if item.contract.strike == Decimal(strike)
        and item.contract.option_type is option_type
    )
