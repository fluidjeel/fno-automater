"""Build option and future FeatureSnapshots from the instrument master.

Lot and tick sizes come only from InstrumentSpec. Missing catalog rows are
skipped rather than guessed.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from trading.data.events import CanonicalMarketEvent
from trading.data.storage.instrument_store import InstrumentSpecStore
from trading.domain.contracts import (
    ContractRef,
    DataQualityReport,
    FeatureSnapshot,
    InstrumentSpec,
    Lineage,
)
from trading.domain.contracts.snapshot import (
    DerivativesContext,
    Greeks,
    MarketQuote,
    SnapshotTimes,
)
from trading.domain.enums import (
    AssetClass,
    Exchange,
    InstrumentKind,
)
from trading.domain.primitives import Price, TickSize

__all__ = [
    "build_future_snapshot",
    "build_option_candidates",
    "front_month_future",
    "priced_from_spec",
]


def front_month_future(
    catalog: InstrumentSpecStore,
    *,
    underlying: str,
    exchange: Exchange,
    as_of: datetime,
    zone: ZoneInfo,
) -> InstrumentSpec | None:
    """Nearest unexpired future for the underlying, or None if the master is empty."""
    today = as_of.astimezone(zone).date()
    matches = [
        spec
        for spec in catalog.list_all()
        if spec.instrument_kind is InstrumentKind.FUTURE
        and spec.underlying == underlying
        and spec.exchange is exchange
        and spec.expiry is not None
        and spec.expiry >= today
        and spec.lot_size > 0
    ]
    if not matches:
        return None
    return min(matches, key=lambda spec: spec.expiry or today)


def build_option_candidates(
    chain: CanonicalMarketEvent,
    catalog: InstrumentSpecStore,
    *,
    underlying: FeatureSnapshot,
    as_of: datetime,
    zone: ZoneInfo,
    strikes_each_side: int,
    quotes: Mapping[str, MarketQuote] | None = None,
) -> tuple[tuple[FeatureSnapshot, ...], dict[str, InstrumentSpec]]:
    """ATM ± N strikes with master-backed InstrumentSpec rows."""
    payload = chain.payload
    rows = payload.get("strikes", [])
    if not isinstance(rows, list):
        return (), {}
    spot = _spot(rows, underlying)
    if spot is None:
        return (), {}
    selected = _atm_window(rows, spot, strikes_each_side)
    quotes = quotes or {}
    snapshots: list[FeatureSnapshot] = []
    instruments: dict[str, InstrumentSpec] = {}
    for row in selected:
        symbol = row.get("symbol")
        if not isinstance(symbol, str) or not symbol:
            continue
        spec = catalog.find(symbol)
        if spec is None or spec.instrument_kind is not InstrumentKind.OPTION:
            continue
        observed_quote = quotes.get(symbol)
        quote = observed_quote or _quote_from_row(row, spec.tick_size)
        if quote is None:
            continue
        snapshot = priced_from_spec(
            spec,
            quote=quote,
            as_of=as_of,
            zone=zone,
            quality=underlying.quality,
            lineage=underlying.lineage,
            times=underlying.times,
            extra_features={
                **underlying.features,
                "top_of_book_observed": Decimal(
                    1
                    if (
                        observed_quote is not None
                        and observed_quote.bid is not None
                        and observed_quote.ask is not None
                    )
                    or (
                        _positive(row.get("bid")) is not None
                        and _positive(row.get("ask")) is not None
                    )
                    else 0
                ),
            },
            feature_set_version=underlying.feature_set_version,
            chain_row=row,
            snapshot_id=f"{underlying.snapshot_id}-{spec.trading_symbol}",
            open_interest=_int_qty(row.get("oi")),
        )
        snapshots.append(snapshot)
        instruments[spec.trading_symbol] = spec
    return tuple(snapshots), instruments


def build_future_snapshot(
    spec: InstrumentSpec,
    *,
    quote: MarketQuote,
    as_of: datetime,
    zone: ZoneInfo,
    quality: DataQualityReport,
    lineage: Lineage,
    times: SnapshotTimes,
    extra_features: Mapping[str, Decimal] | None = None,
    feature_set_version: str,
    open_interest: int | None = None,
) -> FeatureSnapshot:
    """One future book priced from the master, not from remembered lots."""
    return priced_from_spec(
        spec,
        quote=quote,
        as_of=as_of,
        zone=zone,
        quality=quality,
        lineage=lineage,
        times=times,
        extra_features=dict(extra_features or {}),
        feature_set_version=feature_set_version,
        chain_row=None,
        snapshot_id=f"SNAP-{spec.trading_symbol}",
        open_interest=open_interest,
    )


def priced_from_spec(
    spec: InstrumentSpec,
    *,
    quote: MarketQuote,
    as_of: datetime,
    zone: ZoneInfo,
    quality: DataQualityReport,
    lineage: Lineage,
    times: SnapshotTimes,
    extra_features: dict[str, Decimal],
    feature_set_version: str,
    chain_row: dict[str, Any] | None,
    snapshot_id: str,
    open_interest: int | None,
) -> FeatureSnapshot:
    """Attach DerivativesContext from the dated instrument master."""
    today = as_of.astimezone(zone).date()
    expiry = spec.expiry
    days = 0 if expiry is None else max(0, (expiry - today).days)
    extra_features = dict(extra_features)
    extra_features["lot_size"] = Decimal(spec.lot_size)
    greeks = _greeks(chain_row)
    option_type = spec.option_type
    contract = ContractRef(
        exchange=spec.exchange,
        symbol=spec.trading_symbol,
        instrument_kind=spec.instrument_kind,
        asset_class=_asset_class(spec),
        underlying=spec.underlying,
        broker_token=spec.provider_token,
        expiry=spec.expiry,
        strike=spec.strike,
        option_type=option_type,
    )
    derivatives = DerivativesContext(
        days_to_expiry=days,
        open_interest=open_interest,
        option_type=option_type,
        greeks=greeks,
        underlying_price=quote.last,
    )
    return FeatureSnapshot(
        snapshot_id=snapshot_id,
        contract=contract,
        times=times,
        market=quote,
        derivatives=derivatives,
        feature_set_version=feature_set_version,
        features=extra_features,
        quality=quality,
        lineage=lineage,
    )


def _asset_class(spec: InstrumentSpec) -> AssetClass:
    if spec.exchange is Exchange.MCX:
        return AssetClass.COMMODITY
    if spec.instrument_kind is InstrumentKind.OPTION:
        return AssetClass.EQUITY_INDEX
    return AssetClass.EQUITY_INDEX


def _spot(rows: Sequence[Any], underlying: FeatureSnapshot) -> Decimal | None:
    for row in rows:
        if isinstance(row, dict) and row.get("option_type") in {"", None}:
            ltp = row.get("ltp", row.get("fp"))
            if ltp is not None:
                return Decimal(str(ltp))
    last = underlying.market.last
    return last.value if last is not None else None


def _atm_window(
    rows: Sequence[Any],
    spot: Decimal,
    strikes_each_side: int,
) -> list[dict[str, Any]]:
    strikes: set[Decimal] = set()
    legs: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict) or row.get("option_type") not in {"CE", "PE"}:
            continue
        try:
            strike = Decimal(str(row.get("strike_price")))
        except ArithmeticError:
            continue
        if strike <= 0:
            continue
        strikes.add(strike)
        legs.append(row)
    ordered = sorted(strikes, key=lambda value: abs(value - spot))
    keep = set(ordered[: 1 + 2 * strikes_each_side])
    return [row for row in legs if Decimal(str(row.get("strike_price"))) in keep]


def _quote_from_row(row: dict[str, Any], tick: Decimal) -> MarketQuote | None:
    tick_size = TickSize.of(tick)
    last = _positive(row.get("ltp", row.get("lp")))
    bid = _positive(row.get("bid"))
    ask = _positive(row.get("ask"))
    if last is None and bid is None and ask is None:
        return None
    if last is None:
        last = bid if bid is not None else ask
    if last is None:
        return None
    snapped_last = Price.snap(last, tick_size)
    if bid is None:
        bid = last - tick if last > tick else last
    if ask is None:
        ask = last + tick
    if ask <= bid:
        ask = bid + tick
    return MarketQuote(
        last=snapped_last,
        bid=Price.snap(bid, tick_size),
        ask=Price.snap(ask, tick_size),
        volume=_int_qty(row.get("volume")),
    )


def _greeks(row: dict[str, Any] | None) -> Greeks | None:
    if row is None:
        return None
    raw = row.get("greeks")
    greeks = raw if isinstance(raw, dict) else {}
    iv = _signed(greeks.get("iv", row.get("iv")))
    delta = _signed(greeks.get("delta"))
    gamma = _signed(greeks.get("gamma"))
    theta = _signed(greeks.get("theta"))
    vega = _signed(greeks.get("vega"))
    if all(value is None for value in (iv, delta, gamma, theta, vega)):
        return None
    return Greeks(
        model="fyers_chain",
        calculation_version="fyers_chain",
        converged=True,
        implied_volatility=iv,
        delta=delta,
        gamma=gamma,
        theta=theta,
        vega=vega,
    )


def _positive(value: Any) -> Decimal | None:
    parsed = _signed(value)
    if parsed is None or parsed <= 0:
        return None
    return parsed


def _signed(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal, str)):
        try:
            return Decimal(str(value))
        except ArithmeticError:
            return None
    return None


def _int_qty(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = int(Decimal(str(value)))
    except (ArithmeticError, ValueError, TypeError):
        return None
    return parsed if parsed >= 0 else None
