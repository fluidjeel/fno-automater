"""Fyers public instrument master: the authority for lot and tick sizes.

The master is keyed by trading symbol and needs no authentication, so it can be
refreshed outside market hours. Numeric fields arrive as JSON floats and are
converted with `Decimal(str(value))` at this boundary; no float is allowed past
it, because a binary float lot or tick size cannot be validated against an
exchange price grid exactly.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from trading.data.events import RawMarketCapture
from trading.domain.clock import Clock
from trading.domain.contracts import InstrumentSpec
from trading.domain.enums import Exchange, InstrumentKind, OptionType

__all__ = [
    "FyersSymbolMaster",
    "SymbolMasterError",
    "parse_symbol_master",
]

_OPTION_TYPES = {"CE": OptionType.CALL, "PE": OptionType.PUT}


class SymbolMasterError(RuntimeError):
    """Raised when the instrument master cannot be fetched or parsed."""


def _decimal_or_none(value: Any) -> Decimal | None:
    """Parse a strictly positive Decimal from provider JSON, else None."""
    if value is None or isinstance(value, bool) or value == "":
        return None
    if not isinstance(value, (int, float, Decimal, str)):
        return None
    try:
        parsed = Decimal(str(value))
    except ArithmeticError:
        return None
    if not parsed.is_finite() or parsed <= 0:
        return None
    return parsed


def _int_or_none(value: Any) -> int | None:
    if value is None or isinstance(value, bool) or value == "":
        return None
    if not isinstance(value, (int, float, Decimal, str)):
        return None
    try:
        parsed = int(Decimal(str(value)))
    except ArithmeticError:
        return None
    return parsed if parsed > 0 else None


def _expiry_date(value: Any, zone: ZoneInfo) -> date | None:
    """Convert the provider's epoch-second expiry into an exchange-local date."""
    epoch = _int_or_none(value)
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, tz=UTC).astimezone(zone).date()


def _tick_precision(tick: Decimal) -> int:
    """Decimal places implied by the tick grid."""
    exponent = tick.normalize().as_tuple().exponent
    return -exponent if isinstance(exponent, int) and exponent < 0 else 0


def _instrument_kind(
    *,
    option_type: OptionType | None,
    expiry: date | None,
    exchange_instrument_type: int | None,
    index_instrument_type: int,
) -> InstrumentKind:
    if option_type is not None:
        return InstrumentKind.OPTION
    if expiry is not None:
        return InstrumentKind.FUTURE
    if exchange_instrument_type == index_instrument_type:
        return InstrumentKind.INDEX
    return InstrumentKind.STOCK


def parse_symbol_master(
    capture: RawMarketCapture,
    *,
    segment: str,
    exchange: Exchange,
    timezone: str,
    index_instrument_type: int,
    source: str,
    symbols: frozenset[str] | None = None,
) -> tuple[InstrumentSpec, ...]:
    """Map instrument-master rows to InstrumentSpec, skipping unusable rows.

    A row without a positive tick size cannot anchor a price grid, so it is
    skipped rather than defaulted. `symbols` restricts parsing to the contracts
    actually needed, because a full F&O master holds tens of thousands of rows.
    """
    zone = ZoneInfo(timezone)
    specs: list[InstrumentSpec] = []
    for trading_symbol, row in capture.payload.items():
        if symbols is not None and trading_symbol not in symbols:
            continue
        if not isinstance(row, dict):
            continue
        tick = _decimal_or_none(row.get("tickSize"))
        if tick is None:
            continue
        token = row.get("fyToken")
        exchange_token = _int_or_none(row.get("exToken"))
        underlying = row.get("underSym")
        session = row.get("tradingSession")
        last_update = row.get("lastUpdate")
        if not (
            isinstance(token, str)
            and token
            and exchange_token is not None
            and isinstance(underlying, str)
            and underlying
            and isinstance(session, str)
            and session
            and isinstance(last_update, str)
            and last_update
        ):
            continue
        option_type = _OPTION_TYPES.get(str(row.get("optType", "")))
        expiry = _expiry_date(row.get("expiryDate"), zone)
        kind = _instrument_kind(
            option_type=option_type,
            expiry=expiry,
            exchange_instrument_type=_int_or_none(row.get("exInstType")),
            index_instrument_type=index_instrument_type,
        )
        lot = _int_or_none(row.get("minLotSize")) or 0
        strike = _decimal_or_none(row.get("strikePrice"))
        try:
            verified_at = date.fromisoformat(last_update)
        except ValueError:
            continue
        try:
            specs.append(
                InstrumentSpec(
                    trading_symbol=trading_symbol,
                    exchange=exchange,
                    segment=segment,
                    underlying=underlying,
                    instrument_kind=kind,
                    provider_token=token,
                    exchange_token=exchange_token,
                    lot_size=lot,
                    tick_size=tick,
                    price_precision=_tick_precision(tick),
                    freeze_quantity=_int_or_none(row.get("qtyFreeze")),
                    expiry=expiry if kind is not InstrumentKind.STOCK else None,
                    strike=strike if kind is InstrumentKind.OPTION else None,
                    option_type=option_type if kind is InstrumentKind.OPTION else None,
                    trading_session=session,
                    upper_price_band=_decimal_or_none(row.get("upperPrice")),
                    lower_price_band=_decimal_or_none(row.get("lowerPrice")),
                    source=source,
                    verified_at=verified_at,
                )
            )
        except ValueError:
            # A row that fails the contract is unusable reference data. Skipping
            # it keeps the catalog trustworthy; defaulting it would not.
            continue
    return tuple(specs)


class FyersSymbolMaster:
    """Download the public Fyers instrument master for one segment."""

    def __init__(
        self,
        clock: Clock,
        *,
        url_template: str,
        timeout_seconds: float,
    ) -> None:
        self._clock = clock
        self._url_template = url_template
        self._timeout_seconds = timeout_seconds

    def url_for(self, segment: str) -> str:
        return self._url_template.format(segment=segment)

    def fetch(self, segment: str) -> RawMarketCapture:
        """Return the raw master payload for one segment."""
        url = self.url_for(segment)
        received_at = self._clock.now_utc()
        with httpx.Client(timeout=self._timeout_seconds) as client:
            response = client.get(url)
        if response.status_code != httpx.codes.OK:
            raise SymbolMasterError(
                f"instrument master {segment} returned {response.status_code}"
            )
        try:
            decoded: Any = response.json()
        except json.JSONDecodeError as exc:
            raise SymbolMasterError(
                f"instrument master {segment} returned non-JSON"
            ) from exc
        if not isinstance(decoded, dict) or not decoded:
            raise SymbolMasterError(f"instrument master {segment} is empty")
        payload: dict[str, Any] = decoded
        capture_id = hashlib.blake2b(
            json.dumps(payload, sort_keys=True).encode(),
            digest_size=8,
        ).hexdigest()
        return RawMarketCapture(
            capture_id=capture_id,
            provider="fyers",
            endpoint=f"sym_details/{segment}",
            received_at=received_at,
            payload=payload,
            http_status=response.status_code,
        )
