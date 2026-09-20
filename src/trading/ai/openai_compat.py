"""OpenAI-compatible chat completions client for Layer 4. Proposal-only."""

from __future__ import annotations

import json
from http import HTTPStatus
from typing import Any

import httpx

from trading.ai.llm_settings import LlmSettings
from trading.ai.ports import LlmTimeoutError, LlmToolCall, LlmTurn

__all__ = [
    "OpenAICompatLlm",
    "openai_tool_specs",
    "parse_openai_chat_completion",
]


def openai_tool_specs(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Map internal TOOL_SPECS to OpenAI function-calling tools."""
    converted: list[dict[str, Any]] = []
    for spec in tools:
        converted.append(
            {
                "type": "function",
                "function": {
                    "name": spec["name"],
                    "description": spec.get("description", ""),
                    "parameters": spec.get("input_schema", {"type": "object"}),
                },
            }
        )
    return converted


def parse_openai_chat_completion(payload: dict[str, Any]) -> LlmTurn:
    """Parse one chat.completions JSON body into an LlmTurn."""
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("chat completion has no choices")
    first = choices[0]
    if not isinstance(first, dict):
        raise ValueError("chat completion choice is not an object")
    message = first.get("message")
    if not isinstance(message, dict):
        raise ValueError("chat completion message is not an object")
    raw_text = message.get("content")
    text = raw_text if isinstance(raw_text, str) else ""
    raw_reasoning = message.get("reasoning_content")
    reasoning = raw_reasoning if isinstance(raw_reasoning, str) else ""
    calls: list[LlmToolCall] = []
    raw_calls = message.get("tool_calls") or ()
    if isinstance(raw_calls, list):
        for raw in raw_calls:
            if not isinstance(raw, dict):
                continue
            function = raw.get("function")
            if not isinstance(function, dict):
                continue
            name = str(function.get("name") or "")
            arguments = _parse_arguments(function.get("arguments"))
            call_id = str(raw.get("id") or f"call-{len(calls) + 1}")
            calls.append(LlmToolCall(call_id=call_id, name=name, arguments=arguments))
    raw_usage = payload.get("usage")
    usage: dict[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}
    resolved_model = str(payload.get("model") or "")
    prompt_tokens = int(usage.get("prompt_tokens") or 0)
    details = usage.get("prompt_tokens_details")
    cached_from_details = (
        int(details.get("cached_tokens") or 0) if isinstance(details, dict) else 0
    )
    cache_hit = int(usage.get("prompt_cache_hit_tokens") or cached_from_details or 0)
    raw_miss = usage.get("prompt_cache_miss_tokens")
    if raw_miss is not None:
        cache_miss = int(str(raw_miss))
    else:
        cache_miss = max(0, prompt_tokens - cache_hit)
    return LlmTurn(
        text=text,
        tool_calls=tuple(calls),
        input_tokens=prompt_tokens,
        output_tokens=int(usage.get("completion_tokens") or 0),
        reasoning=reasoning,
        resolved_model_id=resolved_model,
        prompt_cache_hit_tokens=cache_hit,
        prompt_cache_miss_tokens=cache_miss,
    )


def _parse_arguments(raw: object) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed: object = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("tool arguments are not JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError("tool arguments must be a JSON object")
    return parsed


def _to_openai_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        if role == "tool":
            converted.append(
                {
                    "role": "tool",
                    "tool_call_id": str(message.get("tool_call_id") or ""),
                    "content": str(message.get("content") or ""),
                }
            )
            continue
        if role == "assistant":
            row: dict[str, Any] = {
                "role": "assistant",
                "content": message.get("content") or None,
            }
            raw_calls = message.get("tool_calls") or ()
            if isinstance(raw_calls, list) and raw_calls:
                row["tool_calls"] = [
                    {
                        "id": str(call.get("id") or ""),
                        "type": "function",
                        "function": {
                            "name": str(call.get("name") or ""),
                            "arguments": _dump_arguments(call.get("arguments")),
                        },
                    }
                    for call in raw_calls
                    if isinstance(call, dict)
                ]
            converted.append(row)
            continue
        converted.append(
            {
                "role": str(role or "user"),
                "content": str(message.get("content") or ""),
            }
        )
    return converted


def _dump_arguments(raw: object) -> str:
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        return json.dumps(raw)
    return "{}"


class OpenAICompatLlm:
    """HTTP client for DeepSeek or any OpenAI-compatible chat API."""

    last_request_body: dict[str, Any] | None
    last_raw_response: dict[str, Any] | None

    def __init__(
        self,
        settings: LlmSettings,
        *,
        timeout_seconds: float = 90.0,
    ) -> None:
        if not settings.deepseek_api_key:
            raise ValueError("DEEPSEEK_API_KEY is empty")
        self._settings = settings
        self._timeout_seconds = timeout_seconds
        self.model = settings.llm_model
        self.last_request_body = None
        self.last_raw_response = None

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> LlmTurn:
        url = self._settings.llm_base_url.rstrip("/") + "/chat/completions"
        body = {
            "model": self._settings.llm_model,
            "messages": _to_openai_messages(messages),
            "tools": openai_tool_specs(tools),
            "tool_choice": "auto",
            "temperature": self._settings.llm_temperature,
            "seed": self._settings.llm_seed,
        }
        self.last_request_body = body
        headers = {
            "Authorization": f"Bearer {self._settings.deepseek_api_key}",
            "Content-Type": "application/json",
        }
        try:
            with httpx.Client(timeout=self._timeout_seconds) as client:
                response = client.post(url, headers=headers, json=body)
        except httpx.TimeoutException as exc:
            raise LlmTimeoutError("LLM HTTP call timed out") from exc
        try:
            payload: dict[str, Any] = response.json()
        except json.JSONDecodeError as exc:
            raise ValueError(f"non-JSON LLM response ({response.status_code})") from exc
        if response.status_code >= HTTPStatus.BAD_REQUEST:
            detail = payload.get("error", payload)
            raise ValueError(f"LLM HTTP {response.status_code}: {detail}")
        self.last_raw_response = payload
        return parse_openai_chat_completion(payload)
