"""Tests for two-way interactive Telegram operator bot."""

from __future__ import annotations

from trading.ops.telegram_bot import TelegramBotConfig, TelegramCommandDispatcher


def test_telegram_dispatcher_authorization() -> None:
    cfg = TelegramBotConfig(token="fake-token", allowed_chat_ids=("12345", "67890"))
    dispatcher = TelegramCommandDispatcher(cfg)

    # Authorized user
    assert dispatcher.is_authorized("12345") is True
    assert dispatcher.is_authorized(12345) is True
    assert dispatcher.is_authorized("67890") is True

    # Unauthorized user
    assert dispatcher.is_authorized("99999") is False
    res = dispatcher.dispatch("/status", "99999")
    assert "Unauthorized" in res


def test_telegram_dispatcher_commands() -> None:
    cfg = TelegramBotConfig(token="fake-token", allowed_chat_ids=("12345",))
    dispatcher = TelegramCommandDispatcher(
        cfg,
        status_provider=lambda: "Phase: MARKET_ACTIVE | Heartbeat: OK",
        positions_provider=lambda: "NIFTY2692425000CE (1 lot, stop=95)",
        blockers_provider=lambda: "0 blockers active",
        flatten_handler=lambda: "All open trades closed & entries frozen",
    )

    # Help
    help_text = dispatcher.dispatch("/help", "12345")
    assert "/status" in help_text
    assert "/positions" in help_text
    assert "/flatten" in help_text

    # Status
    status_text = dispatcher.dispatch("/status", "12345")
    assert "MARKET_ACTIVE" in status_text

    # Positions
    pos_text = dispatcher.dispatch("/positions", "12345")
    assert "NIFTY2692425000CE" in pos_text

    # Blockers
    blockers_text = dispatcher.dispatch("/blockers", "12345")
    assert "0 blockers active" in blockers_text

    # Emergency Flatten
    flatten_text = dispatcher.dispatch("/flatten", "12345")
    assert "All open trades closed" in flatten_text

    # Unknown command
    unknown_text = dispatcher.dispatch("/foo", "12345")
    assert "Unknown command `/foo`" in unknown_text
