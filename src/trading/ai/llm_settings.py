"""LLM credentials from the environment. Secrets never enter domain code."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["LlmSettings", "load_llm_settings"]


class LlmSettings(BaseSettings):
    """OpenAI-compatible model credentials. Loaded from repo-root ``.env``."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    deepseek_api_key: str = Field(default="", alias="DEEPSEEK_API_KEY")
    llm_base_url: str = Field(default="https://api.deepseek.com", alias="LLM_BASE_URL")
    llm_model: str = Field(default="deepseek-chat", alias="LLM_MODEL")

    @classmethod
    def from_repo_root(cls, repo_root: Path) -> LlmSettings:
        """Load from ``<repo>/.env`` so cwd does not matter."""
        env_file = repo_root / ".env"
        if env_file.is_file():
            return cls(_env_file=env_file)  # type: ignore[call-arg]
        return cls()


def load_llm_settings(repo_root: Path) -> LlmSettings:
    """Load LLM settings from the repo-root ``.env`` when present."""
    return LlmSettings.from_repo_root(repo_root)
