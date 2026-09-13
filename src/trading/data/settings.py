"""Broker credentials from the environment. Secrets never enter domain code."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["FyersSettings", "load_fyers_settings"]


class FyersSettings(BaseSettings):
    """Fyers API v3 credentials. Loaded from .env on the laptop or Oracle VM."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    fyers_app_id: str = Field(alias="FYERS_APP_ID")
    fyers_secret_key: str = Field(alias="FYERS_SECRET_KEY")
    fyers_redirect_uri: str = Field(
        default="https://127.0.0.1:5000/",
        alias="FYERS_REDIRECT_URI",
    )
    fyers_access_token: str = Field(default="", alias="FYERS_ACCESS_TOKEN")
    fyers_env: str = Field(default="api", alias="FYERS_ENV")
    data_underlyings: str = Field(
        default="NSE:NIFTY50-INDEX",
        alias="DATA_UNDERLYINGS",
    )

    @field_validator("fyers_env")
    @classmethod
    def _env_is_known(cls, value: str) -> str:
        if value not in {"api", "sandbox"}:
            raise ValueError("FYERS_ENV must be 'api' or 'sandbox'")
        return value

    @property
    def v3_host(self) -> str:
        """Fyers v3 REST + data host (matches ``fyers-apiv3`` Config)."""
        # api.fyers.in serves v2 only (span margin, EDIS). v3 → api-t1 always.
        return "api-t1.fyers.in"

    @property
    def api_base_url(self) -> str:
        """Transaction and auth REST (Phase 2 execution)."""
        return f"https://{self.v3_host}/api/v3"

    @property
    def data_base_url(self) -> str:
        """Market data REST — separate path from ``/api/v3`` on the same host."""
        return f"https://{self.v3_host}/data"

    @property
    def auth_header(self) -> str:
        if not self.fyers_access_token:
            raise ValueError(
                "FYERS_ACCESS_TOKEN is empty. Run 'trading auth fyers' on the "
                "machine that will call the API."
            )
        return f"{self.fyers_app_id}:{self.fyers_access_token}"

    @property
    def underlying_symbols(self) -> tuple[str, ...]:
        return tuple(s.strip() for s in self.data_underlyings.split(",") if s.strip())

    def token_cache_path(self, root: Path) -> Path:
        return root / ".fyers_token"

    def load_cached_token(self, root: Path) -> str | None:
        path = self.token_cache_path(root)
        if path.is_file():
            token = path.read_text(encoding="utf-8").strip()
            return token or None
        return None

    def save_cached_token(self, root: Path, token: str) -> None:
        self.token_cache_path(root).write_text(token.strip(), encoding="utf-8")

    @classmethod
    def from_repo_root(cls, repo_root: Path) -> FyersSettings:
        """Load from ``<repo>/.env`` so cwd does not matter for CLI or systemd."""
        env_file = repo_root / ".env"
        if env_file.is_file():
            return cls(_env_file=env_file)  # type: ignore[call-arg]
        return cls()  # type: ignore[call-arg]


def load_fyers_settings(repo_root: Path | None = None) -> FyersSettings:
    """Load broker settings from the repo-root ``.env`` when present."""
    if repo_root is None:
        return FyersSettings()  # type: ignore[call-arg]
    return FyersSettings.from_repo_root(repo_root)
