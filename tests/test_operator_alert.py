"""Operator Telegram alerts with deduplication."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from trading.ops.operator_alert import notify_operator


def test_notify_operator_sends_once_per_dedupe_key(tmp_path: Path) -> None:
    sent: list[str] = []

    def _fake_send(text: str, *, token: str, chat_id: str) -> bool:
        _ = (token, chat_id)
        sent.append(text)
        return True

    settings = type(
        "Settings",
        (),
        {"a2a_telegram_bot_token": "token", "a2a_telegram_chat_id": "chat"},
    )()

    with (
        patch(
            "trading.ops.operator_alert.FyersSettings.from_repo_root_with_cache",
            return_value=settings,
        ),
        patch(
            "trading.ops.operator_alert.send_telegram_message", side_effect=_fake_send
        ),
    ):
        assert notify_operator(
            tmp_path,
            title="paper down",
            detail="unit inactive",
            dedupe_key="service:fno-paper-session.service",
        )
        assert not notify_operator(
            tmp_path,
            title="paper down",
            detail="unit inactive",
            dedupe_key="service:fno-paper-session.service",
        )

    assert len(sent) == 1
    assert "[FNO OPS] paper down" in sent[0]


def test_notify_operator_respects_cooldown_expiry(tmp_path: Path) -> None:
    dedupe_path = tmp_path / "data" / "ops" / "alert_dedupe.json"
    dedupe_path.parent.mkdir(parents=True)
    stale = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    dedupe_path.write_text('{"service:test": "' + stale + '"}', encoding="utf-8")

    settings = type(
        "Settings",
        (),
        {"a2a_telegram_bot_token": "token", "a2a_telegram_chat_id": "chat"},
    )()

    with (
        patch(
            "trading.ops.operator_alert.FyersSettings.from_repo_root_with_cache",
            return_value=settings,
        ),
        patch("trading.ops.operator_alert.send_telegram_message", return_value=True),
    ):
        assert notify_operator(
            tmp_path,
            title="retry",
            detail="after cooldown",
            dedupe_key="service:test",
            cooldown_seconds=60,
        )
