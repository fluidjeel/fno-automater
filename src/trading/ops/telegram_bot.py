"""Two-way interactive Telegram operator bot.

Provides secure remote monitoring and emergency controls (/status, /positions,
/blockers, /flatten, /help) for the autonomous trading platform.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass

from trading.data.fyers.telegram import get_updates, send_telegram_message

__all__ = [
    "TelegramBotConfig",
    "TelegramCommandDispatcher",
]

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TelegramBotConfig:
    """Authentication and configuration for the Telegram bot."""

    token: str
    allowed_chat_ids: tuple[str, ...]
    poll_timeout_seconds: int = 5


class TelegramCommandDispatcher:
    """Dispatches authorized incoming chat commands to operational actions."""

    def __init__(
        self,
        config: TelegramBotConfig,
        *,
        status_provider: Callable[[], str] | None = None,
        positions_provider: Callable[[], str] | None = None,
        blockers_provider: Callable[[], str] | None = None,
        flatten_handler: Callable[[], str] | None = None,
    ) -> None:
        self._config = config
        self._status_provider = status_provider or (
            lambda: "System status: OK (no provider attached)"
        )
        self._positions_provider = positions_provider or (
            lambda: "Open positions: None"
        )
        self._blockers_provider = blockers_provider or (
            lambda: "Attention blockers: None"
        )
        self._flatten_handler = flatten_handler or (
            lambda: "EMERGENCY: Flatten invoked (simulation)"
        )

    def is_authorized(self, chat_id: str | int) -> bool:
        """Check if chat_id is in the allowed whitelist."""
        return str(chat_id) in self._config.allowed_chat_ids

    def dispatch(self, text: str, chat_id: str | int) -> str:
        """Process incoming command text and return response message."""
        if not self.is_authorized(chat_id):
            logger.warning("Unauthorized access attempt from chat_id %s", chat_id)
            return "⛔ Unauthorized access denied."

        cmd = text.strip().split()[0].lower() if text.strip() else ""

        if cmd in {"/start", "/help"}:
            return (
                "🤖 *Autonomous Trading Bot Commands:*\n"
                "• `/status` - Current operational phase & health\n"
                "• `/positions` - List open positions & stops\n"
                "• `/blockers` - View attention requests & gaps\n"
                "• `/flatten` - Trigger emergency entry freeze & exit\n"
                "• `/help` - Show this menu"
            )

        handlers: dict[str, Callable[[], str]] = {
            "/status": lambda: f"📊 *Status Report:*\n{self._status_provider()}",
            "/positions": lambda: f"📈 *Positions View:*\n{self._positions_provider()}",
            "/blockers": lambda: (
                f"⚠️ *Blockers & Attention:*\n{self._blockers_provider()}"
            ),
            "/flatten": lambda: (
                f"🚨 *Emergency Action Executed:*\n{self._flatten_handler()}"
            ),
        }
        handler = handlers.get(cmd)
        if handler is not None:
            return handler()

        return f"❓ Unknown command `{cmd}`. Type `/help` for available commands."

    def poll_once(self, offset: int | None = None) -> int | None:
        """Fetch and process one batch of updates from Telegram. Returns new offset."""
        if not self._config.token:
            return offset

        updates = get_updates(
            token=self._config.token,
            offset=offset,
            timeout_seconds=self._config.poll_timeout_seconds,
        )
        if not updates:
            return offset

        last_update_id = offset
        for item in updates:
            update_id = item.get("update_id")
            if isinstance(update_id, int):
                last_update_id = update_id + 1

            message = item.get("message") or item.get("channel_post")
            if not isinstance(message, dict):
                continue

            chat = message.get("chat")
            chat_id = str(chat.get("id")) if isinstance(chat, dict) else ""
            text = str(message.get("text") or "")

            if text and chat_id:
                reply = self.dispatch(text, chat_id)
                send_telegram_message(
                    reply,
                    token=self._config.token,
                    chat_id=chat_id,
                )

        return last_update_id

    def run_loop(self, stop_event: threading.Event) -> None:
        """Continuous polling loop."""
        offset: int | None = None
        logger.info("Telegram operator bot polling started.")
        while not stop_event.is_set():
            try:
                offset = self.poll_once(offset)
            except Exception:
                logger.exception("Error polling Telegram updates")
                stop_event.wait(timeout=5.0)
        logger.info("Telegram operator bot polling stopped.")
