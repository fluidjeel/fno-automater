"""Fetch and normalize one option chain snapshot."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

from trading.data.events import RawMarketCapture
from trading.data.minute_backbone.models import ChainSnapshot, ChainStrikeRow, snapshot_id

__all__ = ["build_chain_snapshot"]


def _dec(value: Any) -> str:
    if value is None:
        return "0"
    return str(Decimal(str(value)))


def _opt_type(raw: str) -> Literal["CALL", "PUT"] | None:
    if raw in {"CE", "CALL"}:
        return "CALL"
    if raw in {"PE", "PUT"}:
        return "PUT"
    return None


def build_chain_snapshot(capture: RawMarketCapture, *, symbol: str) -> ChainSnapshot:
    """Map Fyers chain REST capture to ChainSnapshot."""
    data = capture.payload.get("data", capture.payload)
    if not isinstance(data, dict):
        data = {}
    chain = data.get("optionsChain") or data.get("options_chain") or []
    rows: list[ChainStrikeRow] = []
    if isinstance(chain, list):
        for item in chain:
            if not isinstance(item, dict):
                continue
            kind = _opt_type(str(item.get("option_type") or item.get("optionType") or ""))
            if kind is None:
                continue
            strike = _dec(item.get("strike_price") or item.get("strike"))
            rows.append(
                ChainStrikeRow(
                    strike=strike,
                    option_type=kind,
                    ltp=_dec(item.get("ltp")),
                    bid=_dec(item.get("bid")),
                    ask=_dec(item.get("ask")),
                    oi=int(item.get("oi") or 0),
                    volume=int(item.get("volume") or 0),
                    iv=_dec(item.get("iv") or item.get("implied_volatility")),
                    delta=_dec(item.get("delta")),
                    gamma=_dec(item.get("gamma")),
                    vega=_dec(item.get("vega")),
                    theta=_dec(item.get("theta")),
                    symbol=str(item.get("symbol") or item.get("sym") or ""),
                )
            )
    expiry = str(data.get("expiryDate") or data.get("expiry_date") or "")
    body = {"symbol": symbol, "strikes": [row.model_dump() for row in rows]}
    return ChainSnapshot(
        snapshot_id=snapshot_id(body),
        symbol=symbol,
        expiry_date=expiry,
        as_of_utc=capture.received_at.astimezone(UTC),
        strikes=tuple(rows),
    )
