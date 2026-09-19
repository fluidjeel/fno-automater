"""Interactive Fyers OAuth — same flow as blue-green ``tools/get_fyers_token``."""

from __future__ import annotations

import base64
import json
import re
import sys
import time
import webbrowser
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from trading.data.fyers.telegram import (
    get_updates,
    send_telegram_message,
    telegram_configured,
)
from trading.data.settings import FyersSettings
from trading.domain.clock import WallClock

__all__ = [
    "exchange_auth_code",
    "extract_auth_code",
    "get_auth_url",
    "refresh_access_token",
    "run_interactive_auth",
    "run_refresh",
    "run_telegram_auth",
]

_AUTH_CODE_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_AUTH_CODE_IN_URL = re.compile(r"auth_code=([A-Za-z0-9._-]+)")
# Bare codes are long opaque tokens; short strings are almost always paste noise.
_MIN_BARE_AUTH_CODE_LEN = 10
# A JWT is header.payload.signature.
_JWT_SEGMENTS = 3
_REFRESH_URL = "https://api-t1.fyers.in/api/v3/validate-refresh-token"


def extract_auth_code(raw: str) -> str:
    """Extract auth_code from a redirect URL or bare code string."""
    text = raw.strip()
    if "auth_code=" in text:
        parsed = urlparse(text if "://" in text else f"https://x/?{text.lstrip('?')}")
        params = parse_qs(parsed.query)
        code = params.get("auth_code", [""])[0]
        if code:
            return code
    if _AUTH_CODE_RE.fullmatch(text):
        return text
    raise ValueError(
        "Could not parse auth_code. Paste the full redirect URL or the raw code."
    )


def get_auth_url(settings: FyersSettings) -> str:
    """Return the Fyers login URL for the configured app."""
    from fyers_apiv3 import fyersModel

    session = fyersModel.SessionModel(
        client_id=settings.fyers_app_id,
        secret_key=settings.fyers_secret_key,
        redirect_uri=settings.fyers_redirect_uri,
        response_type="code",
        grant_type="authorization_code",
        state="fno-automated-auth",
    )
    auth_url: str = session.generate_authcode()
    return auth_url


def exchange_auth_code(settings: FyersSettings, auth_code: str) -> tuple[str, str]:
    """Exchange an auth_code for ``(access_token, refresh_token)``."""
    from fyers_apiv3 import fyersModel

    session = fyersModel.SessionModel(
        client_id=settings.fyers_app_id,
        secret_key=settings.fyers_secret_key,
        redirect_uri=settings.fyers_redirect_uri,
        grant_type="authorization_code",
    )
    session.set_token(auth_code)
    response = session.generate_token()
    if response.get("s") != "ok":
        raise ValueError(f"Token exchange failed: {response.get('message', response)}")
    access_token = response.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise ValueError("Token exchange returned no access_token")
    refresh_token = response.get("refresh_token", "")
    if not isinstance(refresh_token, str):
        refresh_token = ""
    return access_token, refresh_token


def refresh_access_token(
    settings: FyersSettings,
    *,
    refresh_token: str,
    transport: httpx.BaseTransport | None = None,
) -> str:
    """Exchange a refresh token for a fresh access token (no browser login)."""
    if not settings.fyers_pin:
        raise ValueError("FYERS_PIN is required to refresh the access token")
    with httpx.Client(timeout=15.0, transport=transport) as client:
        response = client.post(
            _REFRESH_URL,
            json={
                "grant_type": "refresh_token",
                "appIdHash": settings.app_id_hash,
                "refresh_token": refresh_token,
                "pin": settings.fyers_pin,
            },
        )
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if payload.get("s") != "ok":
        message = payload.get("message") or f"HTTP {response.status_code}"
        raise ValueError(f"refresh failed: {message}")
    access_token = payload.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise ValueError("refresh returned no access_token")
    return access_token


def _is_sebi_refresh_disabled(message: str) -> bool:
    """True when Fyers rejected refresh because the API is SEBI-disabled."""
    lowered = message.lower()
    return "sebi" in lowered and "disabled" in lowered


def _notify(settings: FyersSettings, text: str) -> bool:
    """Send a Telegram operator alert using the configured credentials."""
    return send_telegram_message(
        text,
        token=settings.a2a_telegram_bot_token,
        chat_id=settings.a2a_telegram_chat_id,
    )


def run_interactive_auth(
    repo_root: Path,
    *,
    auth_code: str = "",
    open_browser: bool = True,
) -> int:
    """Prompt for redirect URL, exchange code, cache token to ``.fyers_token``."""
    print("=" * 60)
    print("Fyers Authentication")
    print("=" * 60)

    try:
        settings = FyersSettings.from_repo_root(repo_root)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        print(
            "\nCreate .env from .env.example with FYERS_APP_ID and FYERS_SECRET_KEY.",
            file=sys.stderr,
        )
        return 1

    if not settings.fyers_app_id or not settings.fyers_secret_key:
        print(
            "ERROR: FYERS_APP_ID and FYERS_SECRET_KEY must be set in .env",
            file=sys.stderr,
        )
        return 1

    print(f"App: {settings.fyers_app_id}")
    print(f"Redirect URI: {settings.fyers_redirect_uri}\n")

    try:
        auth_url = get_auth_url(settings)
    except Exception as exc:
        print(f"ERROR: could not build login URL: {exc}", file=sys.stderr)
        return 1

    print("Step 1: Open this URL and approve the app:\n")
    print("-" * 60)
    print(auth_url)
    print("-" * 60)
    print("\nStep 2: Log in with your Fyers credentials.")
    print("Step 3: Copy the full redirected URL from your browser.\n")

    if open_browser:
        webbrowser.open(auth_url, new=1)

    raw = (
        auth_code.strip()
        if auth_code
        else input("Paste redirect URL or auth_code: ").strip()
    )
    if not raw:
        print("ERROR: no URL or auth_code provided", file=sys.stderr)
        return 1

    try:
        code = extract_auth_code(raw)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"\nFound auth_code: {code[:20]}...")
    print("Exchanging for access token...")

    try:
        token, refresh_token = exchange_auth_code(settings, code)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    settings.save_cached_token(repo_root, token)
    if refresh_token:
        settings.save_refresh_token(repo_root, refresh_token)
    cache_path = settings.token_cache_path(repo_root)
    print(f"\nToken saved to {cache_path}")
    print(f"Token preview: {token[:30]}...")
    if refresh_token:
        print(
            "Refresh token saved, but Fyers may reject silent refresh under "
            "SEBI rules. Re-login via Telegram when the access token expires."
        )
    else:
        print("WARN: no refresh token returned; daily refresh will be unavailable.")
    _notify(settings, "Fyers access token obtained (interactive login).")
    print("\nRun: trading data fetch")
    return 0


def run_refresh(repo_root: Path) -> int:
    """Refresh the access token from the stored refresh token (no browser)."""
    try:
        settings = FyersSettings.from_repo_root(repo_root)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    refresh_token = settings.load_refresh_token(repo_root)
    if not refresh_token:
        print(
            "ERROR: no refresh token cached; run 'trading auth fyers' once first.",
            file=sys.stderr,
        )
        _notify(settings, "Fyers token refresh FAILED: no refresh token cached.")
        return 1

    try:
        access_token = refresh_access_token(settings, refresh_token=refresh_token)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        if _is_sebi_refresh_disabled(str(exc)):
            _notify(
                settings,
                "Fyers silent token refresh is disabled under SEBI rules. "
                "This is expected. Reply to the next login link, or run "
                "`trading auth telegram`, when the access token expires. "
                "The cached access token was not renewed.",
            )
        else:
            _notify(settings, f"Fyers token refresh FAILED: {exc}")
        return 1

    settings.save_cached_token(repo_root, access_token)
    print("Fyers access token refreshed.")
    _notify(settings, "Fyers access token refreshed successfully.")
    return 0


def _auth_code_from_text(text: str) -> str | None:
    """Pull an auth_code out of a pasted redirect URL or a bare code."""
    match = _AUTH_CODE_IN_URL.search(text)
    if match:
        return match.group(1)
    stripped = text.strip()
    if _AUTH_CODE_RE.fullmatch(stripped) and len(stripped) > _MIN_BARE_AUTH_CODE_LEN:
        return stripped
    return None


def _latest_update_id(updates: list[dict[str, Any]]) -> int:
    ids = [
        int(update["update_id"])
        for update in updates
        if isinstance(update.get("update_id"), int)
    ]
    return max(ids, default=0)


def _auth_code_from_update(update: dict[str, Any]) -> str | None:
    message = update.get("message")
    text = message.get("text") if isinstance(message, dict) else None
    if not isinstance(text, str) or not text:
        return None
    return _auth_code_from_text(text)


def _token_expiry(token: str) -> datetime | None:
    """Decode the JWT ``exp`` claim. ``None`` when unparseable."""
    parts = token.split(".")
    if len(parts) != _JWT_SEGMENTS:
        return None
    try:
        padding = "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + padding))
    except (ValueError, json.JSONDecodeError):
        return None
    exp = payload.get("exp") if isinstance(payload, dict) else None
    if not isinstance(exp, int):
        return None
    return datetime.fromtimestamp(exp, tz=UTC)


def _token_is_fresh(settings: FyersSettings, repo_root: Path) -> bool:
    """True when the cached access token is still valid well into the session."""
    cached = settings.load_cached_token(repo_root)
    if not cached:
        return False
    expiry = _token_expiry(cached)
    if expiry is None:
        return False
    return expiry > WallClock().now_utc() + timedelta(minutes=15)


def _complete_login(settings: FyersSettings, repo_root: Path, code: str) -> int:
    """Exchange an auth_code and persist the tokens. Returns process status."""
    try:
        access_token, refresh_token = exchange_auth_code(settings, code)
    except ValueError as exc:
        print(f"Fyers login failed: {exc}", file=sys.stderr)
        return 1
    settings.save_cached_token(repo_root, access_token)
    if refresh_token:
        settings.save_refresh_token(repo_root, refresh_token)
    _notify(settings, "✅ Fyers token refreshed.")
    print("Fyers token refreshed from Telegram reply.")
    return 0


def run_telegram_auth(
    repo_root: Path,
    *,
    timeout_minutes: int = 10,
    poll_seconds: int = 20,
) -> int:
    """Send the login URL to Telegram and exchange the pasted redirect URL."""
    try:
        settings = FyersSettings.from_repo_root(repo_root)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if not telegram_configured(
        settings.a2a_telegram_bot_token, settings.a2a_telegram_chat_id
    ):
        print(
            "ERROR: set A2A_TELEGRAM_BOT_TOKEN and A2A_TELEGRAM_CHAT_ID in .env",
            file=sys.stderr,
        )
        return 1

    if _token_is_fresh(settings, repo_root):
        print("Access token is still valid; no login prompt needed.")
        return 0

    token = settings.a2a_telegram_bot_token
    # A reply to an earlier prompt may still be pending; honor it first.
    pending = get_updates(token=token)
    offset = _latest_update_id(pending) + 1
    for update in pending:
        code = _auth_code_from_update(update)
        if code is not None and _complete_login(settings, repo_root, code) == 0:
            return 0

    try:
        auth_url = get_auth_url(settings)
    except Exception as exc:
        print(f"ERROR: could not build login URL: {exc}", file=sys.stderr)
        _notify(settings, f"Fyers login failed to build URL: {exc}")
        return 1

    sent = _notify(
        settings,
        "Fyers login required.\n\n"
        f"{auth_url}\n\n"
        "Open the link, log in and approve, then paste the full redirect URL "
        "back here (it contains auth_code=...).",
    )
    if not sent:
        print("ERROR: could not send Telegram message", file=sys.stderr)
        return 1
    print("Login URL sent to Telegram; waiting for your reply...")

    deadline = time.monotonic() + timeout_minutes * 60
    while time.monotonic() < deadline:
        updates = get_updates(token=token, offset=offset, timeout_seconds=poll_seconds)
        for update in updates:
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                offset = max(offset, update_id + 1)
            code = _auth_code_from_update(update)
            if code is None:
                continue
            return _complete_login(settings, repo_root, code)

    _notify(settings, "⚠️ Fyers login timed out; token not refreshed.")
    print("Timed out waiting for a Telegram reply.", file=sys.stderr)
    return 1
