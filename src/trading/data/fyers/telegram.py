"""Telegram Bot API notifications for operator alerting.

Credentials are read from ``A2A_TELEGRAM_BOT_TOKEN`` and ``A2A_TELEGRAM_CHAT_ID``.
When either is unset the call is a silent no-op returning ``False``, so alerting
can be wired everywhere without breaking the trading path.
"""

from __future__ import annotations

import os

import httpx

__all__ = ["send_telegram_message"]

_API = "https://api.telegram.org/bot{token}/sendMessage"


def send_telegram_message(text: str, *, timeout_seconds: float = 5.0) -> bool:
    """Post ``text`` to the configured Telegram chat. Returns whether it sent."""
    token = os.getenv("A2A_TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("A2A_TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return False
    try:
        response = httpx.post(
            _API.format(token=token),
            json={"chat_id": chat_id, "text": text},
            timeout=timeout_seconds,
        )
        response.raise_for_status()
        return True
    except httpx.HTTPError:
        return False
