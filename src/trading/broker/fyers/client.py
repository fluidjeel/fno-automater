"""Fyers v3 transaction REST client."""

from __future__ import annotations

import json
from http import HTTPStatus
from typing import Any

import httpx

from trading.broker.ports import BrokerError
from trading.data.settings import FyersSettings

__all__ = ["FyersApiError", "FyersTransactionClient"]

_SPAN_MARGIN_URL = "https://api.fyers.in/api/v2/span_margin"


class FyersApiError(BrokerError):
    """Raised when Fyers returns a non-success transaction response."""


class FyersTransactionClient:
    """Thin HTTP wrapper for Fyers order, position and funds APIs."""

    def __init__(
        self,
        settings: FyersSettings,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self._settings = settings
        self._timeout = timeout_seconds
        self._client = httpx.Client(
            base_url=settings.api_base_url,
            transport=transport,
            timeout=timeout_seconds,
        )

    def close(self) -> None:
        self._client.close()

    def place_order(self, payload: dict[str, object]) -> dict[str, Any]:
        """Place one synchronous order."""
        return self._request("POST", "/orders/sync", json_body=payload)

    def cancel_order(self, broker_order_id: str) -> dict[str, Any]:
        """Cancel one order by broker id."""
        return self._request(
            "DELETE",
            "/orders/sync",
            json_body={"id": broker_order_id},
        )

    def orderbook(
        self,
        *,
        order_id: str | None = None,
        order_tag: str | None = None,
    ) -> dict[str, Any]:
        """Fetch orders, optionally filtered by id or client tag."""
        params: dict[str, str] = {}
        if order_id is not None:
            params["id"] = order_id
        if order_tag is not None:
            params["order_tag"] = f"1:{order_tag}"
        return self._request("GET", "/orders", params=params or None)

    def positions(self) -> dict[str, Any]:
        """Fetch open net positions."""
        return self._request("GET", "/positions")

    def funds(self) -> dict[str, Any]:
        """Fetch account funds and margin."""
        return self._request("GET", "/funds")

    def multiorder_margin(self, legs: list[dict[str, object]]) -> dict[str, Any]:
        """Preflight margin for one or more proposed legs."""
        return self._request(
            "POST",
            "/multiorder/margin",
            json_body={"data": legs},
        )

    def span_margin(self, legs: list[dict[str, object]]) -> dict[str, Any]:
        """Legacy span margin on the v2 host (optional fallback)."""
        headers = {
            "Authorization": self._settings.auth_header,
            "Content-Type": "application/json",
        }
        response = httpx.post(
            _SPAN_MARGIN_URL,
            headers=headers,
            json={"data": legs},
            timeout=self._timeout,
        )
        return _decode_response(response)

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json_body: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        headers = {
            "Authorization": self._settings.auth_header,
            "Content-Type": "application/json",
        }
        response = self._client.request(
            method,
            path,
            params=params,
            json=json_body,
            headers=headers,
        )
        return _decode_response(response)


def _decode_response(response: httpx.Response) -> dict[str, Any]:
    try:
        payload: dict[str, Any] = response.json()
    except json.JSONDecodeError as exc:
        raise FyersApiError(
            f"non-JSON response from Fyers ({response.status_code})"
        ) from exc
    if response.status_code != HTTPStatus.OK:
        message = payload.get("message", response.text)
        raise FyersApiError(f"Fyers HTTP {response.status_code}: {message}")
    return payload
