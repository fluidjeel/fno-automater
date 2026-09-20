"""Agent Desk Prompt Caching & Token Optimizer for Layer 4.

Ensures byte-identical static prompt prefixes across calls for DeepSeek Context
Caching and OpenAI Prompt Caching, achieving 90%+ cost reduction.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

__all__ = [
    "DEFAULT_STATIC_SYSTEM_DIRECTIVE",
    "PromptCacheManager",
    "PromptCacheMetrics",
]

DEFAULT_STATIC_SYSTEM_DIRECTIVE = """You are the Layer 4 Advisory Agent.
PRIMARY INVARIANTS:
1. Live paths (Layers 1-3) are 100% deterministic with hard bounds.
2. Layer 4 operations are strictly proposal-only and shadow-mode.
3. You must NEVER emit direct execution commands or alter risk limits.
4. Output must strictly adhere to the requested JSON schema contracts.
5. In case of ambiguous data or missing evidence, abstain.
"""


@dataclass(frozen=True, slots=True)
class PromptCacheMetrics:
    """Token efficiency metrics for cached prompts."""

    total_prompt_tokens: int
    cached_tokens: int
    uncached_tokens: int

    @property
    def hit_ratio(self) -> float:
        if self.total_prompt_tokens == 0:
            return 0.0
        return self.cached_tokens / self.total_prompt_tokens

    @property
    def estimated_savings_pct(self) -> float:
        """DeepSeek and OpenAI cache hits provide ~90% token discount."""
        if self.total_prompt_tokens == 0:
            return 0.0
        return (self.cached_tokens * 0.9 / self.total_prompt_tokens) * 100.0


class PromptCacheManager:
    """Manages prompt construction for provider prefix caching."""

    def __init__(self, static_directive: str = DEFAULT_STATIC_SYSTEM_DIRECTIVE) -> None:
        self._static_directive = self._normalize_text(static_directive)

    @staticmethod
    def _normalize_text(text: str) -> str:
        """Normalize line endings and outer whitespace."""
        return "\n".join(line.rstrip() for line in text.strip().splitlines())

    def build_system_content(
        self,
        domain_schemas: dict[str, Any] | str | None = None,
    ) -> str:
        """Construct the immutable static system prefix."""
        parts = [self._static_directive]
        if domain_schemas:
            if isinstance(domain_schemas, dict):
                serialized = json.dumps(domain_schemas, sort_keys=True, indent=2)
            else:
                serialized = str(domain_schemas).strip()
            parts.append(f"\n--- DOMAIN CONTRACTS AND SCHEMAS ---\n{serialized}")
        return "\n\n".join(parts)

    def build_messages(
        self,
        dynamic_user_content: str,
        *,
        domain_schemas: dict[str, Any] | str | None = None,
        conversation_history: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Assemble messages with static prefix first to guarantee prompt cache hits."""
        system_content = self.build_system_content(domain_schemas)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_content},
        ]
        if conversation_history:
            messages.extend(conversation_history)
        messages.append({"role": "user", "content": dynamic_user_content})
        return messages

    @staticmethod
    def fingerprint(system_content: str) -> str:
        """Return SHA256 hex digest of the system prefix for cache verification."""
        return hashlib.sha256(system_content.encode("utf-8")).hexdigest()

    @staticmethod
    def evaluate_metrics(
        turn_input_tokens: int, cached_tokens: int
    ) -> PromptCacheMetrics:
        """Calculate token savings and hit ratio from turn metrics."""
        uncached = max(0, turn_input_tokens - cached_tokens)
        return PromptCacheMetrics(
            total_prompt_tokens=turn_input_tokens,
            cached_tokens=cached_tokens,
            uncached_tokens=uncached,
        )
