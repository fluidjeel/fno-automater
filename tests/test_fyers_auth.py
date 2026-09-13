"""Fyers OAuth helpers (offline; no broker calls)."""

from __future__ import annotations

import hashlib
import json

import httpx
import pytest

from trading.data.fyers.auth import extract_auth_code, refresh_access_token
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
