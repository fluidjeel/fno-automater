"""Telegram operator alerts for unattended ops failures."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

from trading.data.fyers.telegram import send_telegram_message, telegram_configured
from trading.data.settings import FyersSettings
from trading.domain.clock import Clock, WallClock

__all__ = ["notify_operator"]

logger = logging.getLogger(__name__)
_PREFIX = "[FNO OPS]"


def notify_operator(
    repo_root: Path,
    *,
    title: str,
    detail: str,
    dedupe_key: str,
    cooldown_seconds: int = 900,
    clock: Clock | None = None,
) -> bool:
    """Send one operator alert. Repeats for the same key are suppressed."""
    wall = clock or WallClock()
    if not _should_send(repo_root, dedupe_key, cooldown_seconds, wall):
        return False
    try:
        settings = FyersSettings.from_repo_root_with_cache(repo_root)
    except Exception as exc:
        logger.warning("operator alert skipped: settings unavailable: %s", exc)
        return False
    if not telegram_configured(
        settings.a2a_telegram_bot_token, settings.a2a_telegram_chat_id
    ):
        logger.warning("operator alert skipped: telegram not configured")
        return False
    text = f"{_PREFIX} {title}\n{detail}"
    sent = send_telegram_message(
        text[:4000],
        token=settings.a2a_telegram_bot_token,
        chat_id=settings.a2a_telegram_chat_id,
    )
    if sent:
        _record_sent(repo_root, dedupe_key, wall)
    return sent


def _dedupe_path(repo_root: Path) -> Path:
    return repo_root / "data" / "ops" / "alert_dedupe.json"


def _should_send(
    repo_root: Path,
    dedupe_key: str,
    cooldown_seconds: int,
    clock: Clock,
) -> bool:
    path = _dedupe_path(repo_root)
    if not path.is_file():
        return True
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return True
    if not isinstance(payload, dict):
        return True
    raw = payload.get(dedupe_key)
    if not isinstance(raw, str):
        return True
    try:
        last = datetime.fromisoformat(raw)
    except ValueError:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    return clock.now_utc() >= last + timedelta(seconds=cooldown_seconds)


def _record_sent(repo_root: Path, dedupe_key: str, clock: Clock) -> None:
    path = _dedupe_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, str] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                payload = {str(key): str(value) for key, value in loaded.items()}
        except (OSError, json.JSONDecodeError):
            payload = {}
    payload[dedupe_key] = clock.now_utc().isoformat()
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
