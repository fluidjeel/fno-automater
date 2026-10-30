"""LLM direction overlay for the M2 agentic agent."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from trading.ai.llm_settings import LlmSettings, load_llm_settings
from trading.ai.openai_compat import OpenAICompatLlm
from trading.ai.m2_agentic_agent.rubric import RubricResult, score_m2_agentic_decision
from trading.ai.ports import LlmTimeoutError
from trading.data.m2_agentic_pipeline.config import M2AgenticPipelineConfig

AgentDirection = Literal["LONG", "SHORT", "NEUTRAL"]

__all__ = [
    "AgentDirectionDecision",
    "decide_agent_direction",
    "parse_llm_direction_payload",
    "resolve_llm_enabled",
]

_SYSTEM_PROMPT = (
    "You read a Nifty decision decision packet JSON. You do not place orders. "
    "Pick exactly one direction for the next five minutes: LONG, SHORT, or NEUTRAL. "
    "A mechanical rubric score is provided as a hint only; you may disagree in paper mode. "
    'Reply with JSON only: {"direction": "LONG" | "SHORT" | "NEUTRAL", '
    '"reasons": ["one short sentence"]}'
)


@dataclass(frozen=True, slots=True)
class AgentDirectionDecision:
    """Final direction plus telemetry fields."""

    direction: AgentDirection
    reasons: tuple[str, ...]
    llm_used: bool
    llm_failure_reason: str | None
    rubric: RubricResult


class _TextLlm(Protocol):
    def complete_text(
        self,
        messages: list[dict[str, Any]],
        *,
        json_object: bool = False,
        model: str | None = None,
        thinking: str | None = None,
    ) -> str: ...


def resolve_llm_enabled(
    config: M2AgenticPipelineConfig,
    *,
    cli_override: bool | None = None,
) -> bool:
    """Config default, overridden by PLATTER_AGENT_LLM_ENABLED then CLI."""
    if cli_override is not None:
        return cli_override
    raw = os.environ.get("PLATTER_AGENT_LLM_ENABLED")
    if raw is not None:
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    return config.agent.llm_enabled


def parse_llm_direction_payload(payload: dict[str, Any]) -> AgentDirection:
    """Validate model JSON direction field."""
    direction = str(payload.get("direction") or "").upper()
    if direction not in {"LONG", "SHORT", "NEUTRAL"}:
        raise ValueError("direction must be LONG, SHORT, or NEUTRAL")
    return direction  # type: ignore[return-value]


def decide_agent_direction(
    platter: dict[str, Any],
    config: M2AgenticPipelineConfig,
    *,
    llm_enabled: bool,
    llm_client: _TextLlm | None = None,
    llm_settings: LlmSettings | None = None,
    repo_root: Any | None = None,
) -> AgentDirectionDecision:
    """Rubric always computed; LLM direction when enabled and healthy."""
    rubric = score_m2_agentic_decision(platter, config)
    if platter.get("fail_closed"):
        reasons = tuple(rubric.reasons + ["stale spot quote; LLM skipped"])
        return AgentDirectionDecision(
            direction="NEUTRAL",
            reasons=reasons,
            llm_used=False,
            llm_failure_reason=None,
            rubric=rubric,
        )
    if not llm_enabled:
        return AgentDirectionDecision(
            direction=rubric.direction,  # type: ignore[arg-type]
            reasons=tuple(rubric.reasons),
            llm_used=False,
            llm_failure_reason=None,
            rubric=rubric,
        )
    try:
        client = llm_client
        if client is None:
            settings = llm_settings or load_llm_settings(repo_root)
            client = OpenAICompatLlm(settings)
        hint = {
            "rubric_direction": rubric.direction,
            "rubric_score_total": str(rubric.score_total),
            "rubric_pillar_scores": rubric.pillar_scores,
            "decision_packet": platter,
        }
        text = client.complete_text(
            [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(hint)},
            ],
            json_object=True,
            model=config.agent.model_id,
            thinking=config.agent.thinking,
        )
        payload = json.loads(text)
        if not isinstance(payload, dict):
            raise ValueError("model reply was not an object")
        llm_direction = parse_llm_direction_payload(payload)
        raw_reasons = payload.get("reasons")
        llm_reasons: list[str] = []
        if isinstance(raw_reasons, list):
            llm_reasons = [str(item).strip()[:200] for item in raw_reasons if str(item).strip()]
        reasons = tuple(
            llm_reasons
            or [f"llm={llm_direction}", f"rubric_hint={rubric.direction}"]
        )
        return AgentDirectionDecision(
            direction=llm_direction,
            reasons=reasons,
            llm_used=True,
            llm_failure_reason=None,
            rubric=rubric,
        )
    except (LlmTimeoutError, ValueError, json.JSONDecodeError, OSError) as exc:
        return AgentDirectionDecision(
            direction=rubric.direction,  # type: ignore[arg-type]
            reasons=tuple(rubric.reasons + [f"llm fallback: {exc}"]),
            llm_used=False,
            llm_failure_reason=str(exc)[:200],
            rubric=rubric,
        )
