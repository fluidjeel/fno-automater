"""Broker payload to canonical events."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

from trading.data.events import CanonicalMarketEvent, RawMarketCapture

__all__ = [
    "normalize_fyers_depth",
    "normalize_fyers_history",
    "normalize_fyers_instrument_reference",
    "normalize_fyers_market_status",
    "normalize_fyers_option_chain",
    "normalize_fyers_quotes",
    "normalize_fyers_ws_tick",
]

# Fyers history candle columns: [ts, open, high, low, close, volume, oi?].
_CANDLE_VOLUME_INDEX = 5
_CANDLE_OI_INDEX = 6


def _parse_epoch_seconds(value: Any, fallback: datetime) -> datetime:
    if value is None:
        return fallback
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=UTC)
    if isinstance(value, str) and value.isdigit():
        return datetime.fromtimestamp(int(value), tz=UTC)
    return fallback


def _event_id(prefix: str, capture_id: str, suffix: str = "") -> str:
    if suffix:
        return f"fyers-{prefix}-{capture_id}-{suffix}"
    return f"fyers-{prefix}-{capture_id}"


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def normalize_fyers_option_chain(
    capture: RawMarketCapture,
    *,
    symbol: str,
    normalization_version: str,
    raw_ref: str,
) -> CanonicalMarketEvent:
    """Map one Fyers option-chain response to a single canonical snapshot event."""
    data = capture.payload.get("data", capture.payload)
    data_dict = _as_dict(data)
    source_time = _parse_epoch_seconds(data_dict.get("timestamp"), capture.received_at)
    rows: list[dict[str, Any]] = []
    chain = data_dict.get("optionsChain") or data_dict.get("options_chain") or []
    if isinstance(chain, list):
        rows = [row for row in chain if isinstance(row, dict)]
    legs = [row for row in rows if row.get("option_type") in {"CE", "PE"}]
    payload = {
        "underlying_symbol": symbol,
        "strike_count": len(legs),
        "strikes": rows,
        "expiry_data": data_dict.get("expiryData"),
        "call_oi": data_dict.get("callOi"),
        "put_oi": data_dict.get("putOi"),
        "india_vix": data_dict.get("indiavixData"),
        "greeks_model": "fyers_chain",
    }
    return CanonicalMarketEvent(
        event_id=_event_id("chain", capture.capture_id),
        provider="fyers",
        symbol=symbol,
        event_type="OPTION_CHAIN_SNAPSHOT",
        event_time=source_time,
        source_time=source_time,
        receive_time=capture.received_at,
        provider_sequence=None,
        payload=payload,
        raw_ref=raw_ref,
        normalization_version=normalization_version,
    )


def normalize_fyers_quotes(
    capture: RawMarketCapture,
    *,
    symbol: str,
    normalization_version: str,
    raw_ref: str,
) -> CanonicalMarketEvent:
    """Map one Fyers quotes response to a canonical quote snapshot."""
    quotes: list[dict[str, Any]] = []
    rows = capture.payload.get("d", [])
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict) and row.get("n") == symbol:
                value = row.get("v")
                if isinstance(value, dict):
                    quotes.append({"symbol": symbol, **value})
    payload = {
        "symbol": symbol,
        "quote_count": len(quotes),
        "quotes": quotes,
    }
    return CanonicalMarketEvent(
        event_id=_event_id("quote", capture.capture_id),
        provider="fyers",
        symbol=symbol,
        event_type="QUOTE_SNAPSHOT",
        event_time=capture.received_at,
        source_time=capture.received_at,
        receive_time=capture.received_at,
        provider_sequence=None,
        payload=payload,
        raw_ref=raw_ref,
        normalization_version=normalization_version,
    )


def normalize_fyers_history(
    capture: RawMarketCapture,
    *,
    symbol: str,
    resolution: str,
    normalization_version: str,
    raw_ref: str,
) -> CanonicalMarketEvent:
    """Map one Fyers history response to a canonical bar snapshot."""
    candles: list[list[Any]] = []
    raw_candles = capture.payload.get("candles", [])
    if isinstance(raw_candles, list):
        candles = [row for row in raw_candles if isinstance(row, list)]
    source_time = capture.received_at
    if candles:
        source_time = _parse_epoch_seconds(candles[-1][0], capture.received_at)
    bars = []
    for row in candles:
        bar: dict[str, Any] = {
            "timestamp": row[0],
            "open": row[1],
            "high": row[2],
            "low": row[3],
            "close": row[4],
            "volume": (
                row[_CANDLE_VOLUME_INDEX] if len(row) > _CANDLE_VOLUME_INDEX else 0
            ),
        }
        if len(row) > _CANDLE_OI_INDEX:
            bar["oi"] = row[_CANDLE_OI_INDEX]
        bars.append(bar)
    payload = {
        "symbol": symbol,
        "resolution": resolution,
        "bar_count": len(bars),
        "bars": bars,
        "is_final": True,
    }
    return CanonicalMarketEvent(
        event_id=_event_id("bar", capture.capture_id, resolution),
        provider="fyers",
        symbol=symbol,
        event_type="BAR_SNAPSHOT",
        event_time=source_time,
        source_time=source_time,
        receive_time=capture.received_at,
        provider_sequence=None,
        payload=payload,
        raw_ref=raw_ref,
        normalization_version=normalization_version,
    )


def normalize_fyers_depth(
    capture: RawMarketCapture,
    *,
    symbol: str,
    normalization_version: str,
    raw_ref: str,
) -> CanonicalMarketEvent:
    """Map one Fyers depth response to a canonical depth snapshot."""
    data = capture.payload.get("d", capture.payload.get("data", capture.payload))
    book = _as_dict(data)
    if symbol in book and isinstance(book[symbol], dict):
        book = book[symbol]
    bids = book.get("bids") or book.get("bid") or []
    asks = book.get("ask") or book.get("asks") or []
    bid_rows = (
        [row for row in bids if isinstance(row, dict)] if isinstance(bids, list) else []
    )
    ask_rows = (
        [row for row in asks if isinstance(row, dict)] if isinstance(asks, list) else []
    )
    payload = {
        "symbol": symbol,
        "bid_levels": bid_rows,
        "ask_levels": ask_rows,
        "bid_count": len(bid_rows),
        "ask_count": len(ask_rows),
        "total_buy_qty": book.get("totalbuyqty"),
        "total_sell_qty": book.get("totalsellqty"),
        "ltp": book.get("ltp"),
        "oi": book.get("oi"),
    }
    return CanonicalMarketEvent(
        event_id=_event_id("depth", capture.capture_id),
        provider="fyers",
        symbol=symbol,
        event_type="DEPTH_SNAPSHOT",
        event_time=capture.received_at,
        source_time=capture.received_at,
        receive_time=capture.received_at,
        provider_sequence=None,
        payload=payload,
        raw_ref=raw_ref,
        normalization_version=normalization_version,
    )


def normalize_fyers_market_status(
    capture: RawMarketCapture,
    *,
    symbol: str,
    normalization_version: str,
    raw_ref: str,
    segment: str,
) -> CanonicalMarketEvent:
    """Map Fyers marketStatus to a canonical status event."""
    rows = capture.payload.get("marketStatus") or capture.payload.get("data") or []
    statuses = (
        [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
    )
    matched: dict[str, Any] | None = None
    for row in statuses:
        exchange = str(row.get("exchange", ""))
        seg = str(row.get("segment", ""))
        if segment in {seg, f"{exchange}_{seg}", exchange}:
            matched = row
            break
        if "FO" in segment and "FO" in f"{exchange}{seg}":
            matched = row
            break
    payload = {
        "symbol": symbol,
        "segment": segment,
        "status": matched.get("status") if matched else None,
        "market_type": matched.get("market_type") if matched else None,
        "rows": statuses,
    }
    return CanonicalMarketEvent(
        event_id=_event_id("status", capture.capture_id),
        provider="fyers",
        symbol=symbol,
        event_type="MARKET_STATUS",
        event_time=capture.received_at,
        source_time=capture.received_at,
        receive_time=capture.received_at,
        provider_sequence=None,
        payload=payload,
        raw_ref=raw_ref,
        normalization_version=normalization_version,
    )


def normalize_fyers_instrument_reference(
    chain_capture: RawMarketCapture,
    *,
    symbol: str,
    normalization_version: str,
    raw_ref: str,
    quote_capture: RawMarketCapture | None = None,
    expiry_capture: RawMarketCapture | None = None,
) -> CanonicalMarketEvent:
    """Build instrument/expiry reference from chain, quotes and optional expiry API."""
    data = _as_dict(chain_capture.payload.get("data", chain_capture.payload))
    expiries: list[dict[str, Any]] = []
    expiry_rows = data.get("expiryData")
    if isinstance(expiry_rows, list):
        for row in expiry_rows:
            if not isinstance(row, dict):
                continue
            expiries.append(
                {
                    "date": row.get("date"),
                    "epoch": row.get("expiry"),
                    "expiry_flag": row.get("expiry_flag"),
                }
            )
    if expiry_capture is not None:
        extra = expiry_capture.payload.get("data", expiry_capture.payload)
        extra_rows = extra.get("expiryData") if isinstance(extra, dict) else extra
        if isinstance(extra_rows, list) and extra_rows:
            enriched: list[dict[str, Any]] = []
            for row in extra_rows:
                if isinstance(row, dict):
                    enriched.append(row)
            if enriched:
                expiries = enriched
    tick_size: str | None = None
    lot_size: int | None = None
    if quote_capture is not None:
        rows = quote_capture.payload.get("d", [])
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict) and row.get("n") == symbol:
                    value = row.get("v")
                    if isinstance(value, dict):
                        tick = value.get("tick_size")
                        if tick is not None:
                            tick_size = str(tick)
                        lot = value.get("lot_size")
                        if isinstance(lot, int):
                            lot_size = lot
    source_time = _parse_epoch_seconds(data.get("timestamp"), chain_capture.received_at)
    payload = {
        "underlying_symbol": symbol,
        "expiry_count": len(expiries),
        "expiries": expiries,
        "tick_size": tick_size,
        "lot_size": lot_size,
        "expiry_source": "expiry" if expiry_capture is not None else "chain",
    }
    return CanonicalMarketEvent(
        event_id=_event_id("ref", chain_capture.capture_id),
        provider="fyers",
        symbol=symbol,
        event_type="INSTRUMENT_REFERENCE",
        event_time=source_time,
        source_time=source_time,
        receive_time=chain_capture.received_at,
        provider_sequence=None,
        payload=payload,
        raw_ref=raw_ref,
        normalization_version=normalization_version,
    )


def normalize_fyers_ws_tick(
    message: dict[str, Any],
    *,
    symbol: str,
    normalization_version: str,
    receive_time: datetime,
    provider_sequence: int | None = None,
) -> CanonicalMarketEvent:
    """Map one Fyers websocket tick payload to a canonical tick event."""
    tick_id = hashlib.blake2b(
        repr(sorted(message.items())).encode(),
        digest_size=8,
    ).hexdigest()
    payload = {
        "symbol": symbol,
        "ltp": message.get("ltp"),
        "bid_price": message.get("bid_price"),
        "ask_price": message.get("ask_price"),
        "volume": message.get("vol_traded_today"),
        "open_price": message.get("open_price"),
        "high_price": message.get("high_price"),
        "low_price": message.get("low_price"),
        "prev_close_price": message.get("prev_close_price"),
    }
    return CanonicalMarketEvent(
        event_id=f"fyers-tick-{tick_id}",
        provider="fyers",
        symbol=symbol,
        event_type="TICK",
        event_time=receive_time,
        source_time=receive_time,
        receive_time=receive_time,
        provider_sequence=provider_sequence,
        payload=payload,
        raw_ref="ws:fyers",
        normalization_version=normalization_version,
    )
