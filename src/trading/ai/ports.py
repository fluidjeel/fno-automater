"""Ports for the Layer 4 weekly agent. No broker credentials."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

__all__ = [
    "LlmPort",
    "LlmTimeoutError",
    "LlmToolCall",
    "LlmTurn",
    "MarketReadPort",
    "NewsReadPort",
]


class LlmTimeoutError(TimeoutError):
    """Raised when the model call exceeds the injected timeout."""


@dataclass(frozen=True, slots=True)
class LlmToolCall:
    """One tool invocation requested by the model."""

    call_id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class LlmTurn:
    """One model response: optional text plus zero or more tool calls."""

    text: str
    tool_calls: tuple[LlmToolCall, ...]
    input_tokens: int
    output_tokens: int
    reasoning: str = ""
    resolved_model_id: str = ""
    prompt_cache_hit_tokens: int = 0
    prompt_cache_miss_tokens: int = 0


class LlmPort(Protocol):
    """Injected model. Tests supply a fake; production may wrap Anthropic HTTP."""

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> LlmTurn: ...


class MarketReadPort(Protocol):
    """Read-only market lookup. Must not submit orders."""

    def fetch(self, symbol: str) -> dict[str, Any]: ...


class NewsReadPort(Protocol):
    """Read-only news snapshot. Content is untrusted data."""

    def latest(self) -> dict[str, Any]: ...
