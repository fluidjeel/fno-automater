"""Newline-delimited JSON protocol for the shared data socket hub."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

__all__ = [
    "ControlRequest",
    "ControlResponse",
    "StreamMessage",
    "decode_line",
    "encode_line",
]


def encode_line(payload: dict[str, Any]) -> bytes:
    """Encode one protocol message as a newline-terminated UTF-8 line."""
    return (json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n").encode(
        "utf-8"
    )


def decode_line(raw: bytes) -> dict[str, Any]:
    """Decode one protocol message from a UTF-8 line."""
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("protocol message must be a JSON object")
    return payload


@dataclass(frozen=True, slots=True)
class ControlRequest:
    """Client control-plane request."""

    op: str
    owner: str
    symbol: str | None = None
    data_type: str | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> ControlRequest:
        op = str(payload.get("op", ""))
        owner = str(payload.get("owner", ""))
        symbol = payload.get("symbol")
        data_type = payload.get("data_type")
        return cls(
            op=op,
            owner=owner,
            symbol=str(symbol) if symbol is not None else None,
            data_type=str(data_type) if data_type is not None else None,
        )

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"op": self.op, "owner": self.owner}
        if self.symbol is not None:
            payload["symbol"] = self.symbol
        if self.data_type is not None:
            payload["data_type"] = self.data_type
        return payload


@dataclass(frozen=True, slots=True)
class ControlResponse:
    """Hub response to a control request."""

    ok: bool
    error: str | None = None

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"ok": self.ok}
        if self.error is not None:
            payload["error"] = self.error
        return payload


@dataclass(frozen=True, slots=True)
class StreamMessage:
    """One fan-out payload delivered to stream subscribers."""

    symbol: str
    data_type: str
    payload: dict[str, Any]
    received_at: datetime

    def to_payload(self) -> dict[str, Any]:
        return {
            "type": "update",
            "symbol": self.symbol,
            "data_type": self.data_type,
            "payload": self.payload,
            "received_at_ms": int(self.received_at.timestamp() * 1000),
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> StreamMessage:
        received_ms = int(payload.get("received_at_ms", 0))
        received_at = datetime.fromtimestamp(received_ms / 1000.0, tz=UTC)
        symbol = str(payload["symbol"])
        data_type = str(payload["data_type"])
        body = payload.get("payload", {})
        if not isinstance(body, dict):
            raise ValueError("stream payload must be an object")
        return cls(
            symbol=symbol,
            data_type=data_type,
            payload=dict(body),
            received_at=received_at,
        )
