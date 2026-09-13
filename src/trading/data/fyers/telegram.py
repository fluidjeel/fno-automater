"""Telegram Bot API helpers for operator alerting and the daily login handoff.

Credentials are passed explicitly (from ``FyersSettings``) so the loader is the
single source of truth and the helpers never read ``.env`` themselves. An empty
token turns sends into a no-op returning ``False`` and updates into ``[]``.
"""

from __future__ import annotations

from typing import Any

import httpx

__all__ = [
    "get_updates",
    "send_telegram_message",
    "telegram_configured",
]

_API = "https://api.telegram.org/bot{token}"


def telegram_configured(token: str, chat_id: str) -> bool:
    """Whether both the bot token and a chat id are present."""
    return bool(token and chat_id)


def send_telegram_message(
    text: str,
    *,
    token: str,
    chat_id: str,
    timeout_seconds: float = 5.0,
) -> bool:
    """Post ``text`` to ``chat_id``. Returns whether it was sent."""
    if not token or not chat_id:
        return False
    try:
        response = httpx.post(
            f"{_API.format(token=token)}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=timeout_seconds,
        )
        response.raise_for_status()
        return True
    except httpx.HTTPError:
        return False


def get_updates(
    *,
    token: str,
    offset: int | None = None,
    timeout_seconds: int = 0,
) -> list[dict[str, Any]]:
    """Long-poll ``getUpdates``. Returns an empty list on error or no token."""
    if not token:
        return []
    params: dict[str, int] = {"timeout": timeout_seconds}
    if offset is not None:
        params["offset"] = offset
    try:
        response = httpx.get(
            f"{_API.format(token=token)}/getUpdates",
            params=params,
            timeout=float(timeout_seconds + 10),
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            return []
        result = payload.get("result", [])
        return result if isinstance(result, list) else []
    except (httpx.HTTPError, ValueError):
        return []
