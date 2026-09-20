"""Shared role-scoped tool-loop runtime (ADESK-B1).

Weekly and advise desks call this; desk-specific prompts, tool specs, and
fail-closed builders stay in loop.py / advise.py. When a grant is supplied and
resolve_effective_mode returns OBSERVE, the model is not called.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum, unique
from typing import Any

from trading.ai.authority import resolve_effective_mode
from trading.ai.budget import TokenBudget
from trading.ai.ports import LlmPort, LlmTimeoutError, LlmTurn
from trading.ai.tools import ToolContext, dispatch_tool
from trading.config.agent import AgentConfig
from trading.domain.contracts.authority import AuthorityGrant
from trading.domain.enums import AuthorityMode, DeskRole

__all__ = [
    "DeskRuntimeSpec",
    "ToolLoopStop",
    "run_desk_tool_loop",
]


@unique
class ToolLoopStop(StrEnum):
    """Why the shared tool loop returned."""

    DISABLED = "DISABLED"
    OBSERVE = "OBSERVE"
    BUDGET = "BUDGET"
    TIMEOUT = "TIMEOUT"
    SCHEMA = "SCHEMA"
    TERMINAL = "TERMINAL"
    EMPTY = "EMPTY"


@dataclass(frozen=True, slots=True)
class DeskRuntimeSpec:
    """Role-scoped prompt + tools for one desk invocation."""

    role: DeskRole
    system_prompt: str
    tool_specs: Sequence[Mapping[str, Any]]
    max_iterations: int
    terminal_tool_names: frozenset[str]


def run_desk_tool_loop(  # noqa: PLR0911
    *,
    llm: LlmPort,
    config: AgentConfig,
    tools: ToolContext,
    prompt: str,
    spec: DeskRuntimeSpec,
    is_terminal: Callable[[ToolContext, str], bool],
    grant: AuthorityGrant | None = None,
    runtime_model_id: str | None = None,
    runtime_prompt_version: str = "",
    runtime_policy_version: str = "",
    now: datetime | None = None,
) -> ToolLoopStop:
    """Run the shared gather/act loop. Caller maps stop to a domain result.

    When grant is None, behaviour matches pre-B1 (config.enabled gates the run).
    When grant is provided, demotion to OBSERVE skips the model.
    """
    if not config.enabled:
        return ToolLoopStop.DISABLED

    if grant is not None:
        mode = resolve_effective_mode(
            spec.role,
            runtime_model_id=runtime_model_id
            or tools.resolved_model_id
            or tools.model_name,
            runtime_prompt_version=runtime_prompt_version,
            runtime_policy_version=runtime_policy_version,
            now=now or tools.clock.now_utc(),
            grant=grant,
        )
        if mode is AuthorityMode.OBSERVE:
            return ToolLoopStop.OBSERVE

    budget = TokenBudget(
        config,
        role=tools.agent_role,
        store=tools.budget_store,
        clock=tools.clock,
    )
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": spec.system_prompt},
        {"role": "user", "content": prompt},
    ]
    iterations = max(0, int(spec.max_iterations))
    try:
        for _ in range(iterations):
            turn = llm.complete(list(messages), [dict(s) for s in spec.tool_specs])
            if turn.resolved_model_id:
                tools.resolved_model_id = turn.resolved_model_id
            if not budget.charge(turn.input_tokens, turn.output_tokens):
                return ToolLoopStop.BUDGET
            if not turn.tool_calls:
                break
            messages.append(_assistant_message(turn))
            for call in turn.tool_calls:
                result = dispatch_tool(call.name, call.arguments, tools)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.call_id,
                        "name": call.name,
                        "content": result,
                    }
                )
                if call.name in spec.terminal_tool_names and is_terminal(
                    tools, call.name
                ):
                    return ToolLoopStop.TERMINAL
        return ToolLoopStop.EMPTY
    except LlmTimeoutError:
        return ToolLoopStop.TIMEOUT
    except (ValueError, TypeError, KeyError):
        return ToolLoopStop.SCHEMA


def _assistant_message(turn: LlmTurn) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": turn.text,
        "tool_calls": [
            {
                "id": call.call_id,
                "name": call.name,
                "arguments": call.arguments,
            }
            for call in turn.tool_calls
        ],
    }
