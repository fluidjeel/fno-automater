"""Fyers OAuth helpers (offline; no broker calls)."""

from __future__ import annotations

import pytest

from trading.data.fyers.auth import extract_auth_code
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
