"""Fyers OAuth helpers (offline; no broker calls)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import httpx
import pytest

from trading.data.fyers import auth as auth_mod
from trading.data.fyers.auth import (
    _auth_code_from_text,
    extract_auth_code,
    refresh_access_token,
    run_telegram_auth,
)
from trading.data.settings import FyersSettings


class TestExtractAuthCode:
    def test_parses_full_redirect_url(self) -> None:
        url = "https://google.com/?auth_code=abc123.xyz&state=foo"
        assert extract_auth_code(url) == "abc123.xyz"

    def test_parses_bare_auth_code(self) -> None:
        assert extract_auth_code("abc123.xyz-TOKEN_part") == "abc123.xyz-TOKEN_part"

    def test_rejects_garbage(self) -> None:
        with pytest.raises(ValueError, match="Could not parse"):
            extract_auth_code("not a valid code or url")


class TestFyersSettingsUrls:
    def test_data_host_is_not_under_api_v3(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Fyers data endpoints use /data, not /api/v3/data (wrong path → 500)."""
        monkeypatch.setenv("FYERS_APP_ID", "APP-100")
        monkeypatch.setenv("FYERS_SECRET_KEY", "secret")
        settings = FyersSettings()  # type: ignore[call-arg]
        assert settings.data_base_url == "https://api-t1.fyers.in/data"
        assert settings.api_base_url == "https://api-t1.fyers.in/api/v3"

    def test_app_id_hash_is_sha256_of_appid_and_secret(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("FYERS_APP_ID", "APP-100")
        monkeypatch.setenv("FYERS_SECRET_KEY", "secret")
        settings = FyersSettings()  # type: ignore[call-arg]
        assert settings.app_id_hash == hashlib.sha256(b"APP-100:secret").hexdigest()


def _settings_with_pin(
    monkeypatch: pytest.MonkeyPatch,
    *,
    pin: str = "1234",
) -> FyersSettings:
    monkeypatch.setenv("FYERS_APP_ID", "APP-100")
    monkeypatch.setenv("FYERS_SECRET_KEY", "secret")
    monkeypatch.setenv("FYERS_PIN", pin)
    return FyersSettings()  # type: ignore[call-arg]


class TestRefreshAccessToken:
    def test_posts_app_id_hash_refresh_token_and_pin(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        settings = _settings_with_pin(monkeypatch)
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json={"s": "ok", "access_token": "new-token"})

        token = refresh_access_token(
            settings,
            refresh_token="refresh-123",
            transport=httpx.MockTransport(handler),
        )
        assert token == "new-token"
        assert (
            captured["url"] == "https://api-t1.fyers.in/api/v3/validate-refresh-token"
        )
        assert captured["body"] == {
            "grant_type": "refresh_token",
            "appIdHash": hashlib.sha256(b"APP-100:secret").hexdigest(),
            "refresh_token": "refresh-123",
            "pin": "1234",
        }

    def test_requires_pin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        settings = _settings_with_pin(monkeypatch, pin="")
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json={}))
        with pytest.raises(ValueError, match="FYERS_PIN"):
            refresh_access_token(
                settings, refresh_token="refresh-123", transport=transport
            )


class TestAuthCodeFromText:
    def test_extracts_from_redirect_url(self) -> None:
        url = "https://google.com/?auth_code=abc123.xyz&state=foo"
        assert _auth_code_from_text(url) == "abc123.xyz"

    def test_extracts_with_surrounding_text(self) -> None:
        assert _auth_code_from_text("here: https://x.com/?auth_code=abc123.xyz") == (
            "abc123.xyz"
        )

    def test_accepts_bare_code(self) -> None:
        assert _auth_code_from_text("abc123.xyz-TOKEN_part") == "abc123.xyz-TOKEN_part"

    def test_rejects_garbage(self) -> None:
        assert _auth_code_from_text("not a code") is None


class TestRunTelegramAuth:
    def test_exchanges_pasted_url_and_saves_token(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setenv("FYERS_APP_ID", "APP-100")
        monkeypatch.setenv("FYERS_SECRET_KEY", "secret")
        monkeypatch.setenv("FYERS_PIN", "1234")

        monkeypatch.setattr(
            auth_mod, "telegram_configured", lambda token, chat_id: True
        )
        monkeypatch.setattr(auth_mod, "get_auth_url", lambda settings: "https://auth")
        monkeypatch.setattr(
            auth_mod,
            "send_telegram_message",
            lambda text, *, token, chat_id, timeout_seconds=5.0: True,
        )
        monkeypatch.setattr(
            auth_mod,
            "get_updates",
            lambda *, token, offset=None, timeout_seconds=0: [
                {"update_id": 7, "message": {"text": "https://x/?auth_code=CODE.123"}}
            ],
        )
        monkeypatch.setattr(
            auth_mod,
            "exchange_auth_code",
            lambda settings, code: ("new-access-token", "new-refresh-token"),
        )

        assert run_telegram_auth(tmp_path) == 0
        assert (tmp_path / ".fyers_token").read_text() == "new-access-token"
        assert (tmp_path / ".fyers_refresh_token").read_text() == "new-refresh-token"
