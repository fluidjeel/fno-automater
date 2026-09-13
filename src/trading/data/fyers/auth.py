"""Interactive Fyers OAuth — same flow as blue-green ``tools/get_fyers_token``."""

from __future__ import annotations

import re
import sys
import webbrowser
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

from trading.data.fyers.telegram import send_telegram_message
from trading.data.settings import FyersSettings

__all__ = [
    "exchange_auth_code",
    "extract_auth_code",
    "get_auth_url",
    "refresh_access_token",
    "run_interactive_auth",
    "run_refresh",
]

_AUTH_CODE_RE = re.compile(r"^[A-Za-z0-9._-]+$")
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
                "appIdHash": settings.app_id_hash,
                "refresh_token": refresh_token,
                "pin": settings.fyers_pin,
            },
        )
    response.raise_for_status()
    payload = response.json()
    if payload.get("s") != "ok":
        raise ValueError(f"refresh failed: {payload.get('message', payload)}")
    access_token = payload.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise ValueError("refresh returned no access_token")
    return access_token


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
        print("Refresh token saved; run 'trading auth refresh' daily.")
    else:
        print("WARN: no refresh token returned; daily refresh will be unavailable.")
    send_telegram_message("Fyers access token obtained (interactive login).")
    print("\nRun: trading data fetch")
    return 0


def run_refresh(repo_root: Path) -> int:
    """Refresh the access token from the stored refresh token (no browser)."""
    try:
        settings = FyersSettings.from_repo_root(repo_root)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        send_telegram_message(f"Fyers token refresh FAILED: {exc}")
        return 1

    refresh_token = settings.load_refresh_token(repo_root)
    if not refresh_token:
        print(
            "ERROR: no refresh token cached; run 'trading auth fyers' once first.",
            file=sys.stderr,
        )
        send_telegram_message("Fyers token refresh FAILED: no refresh token cached.")
        return 1

    try:
        access_token = refresh_access_token(settings, refresh_token=refresh_token)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        send_telegram_message(f"Fyers token refresh FAILED: {exc}")
        return 1

    settings.save_cached_token(repo_root, access_token)
    print("Fyers access token refreshed.")
    send_telegram_message("Fyers access token refreshed successfully.")
    return 0
