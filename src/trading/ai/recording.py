"""Persist Layer 4 agent reasoning. Advisory artifacts only."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from trading.ai.ports import LlmPort, LlmTurn
from trading.domain.contracts import AIProposal, AttentionRequest

__all__ = ["RecordingLlm", "persist_agent_run"]


class RecordingLlm:
    """Wrap an LLM and keep every turn for the audit transcript."""

    inner: LlmPort
    turns: list[dict[str, Any]]
    resolved_model_id: str

    def __init__(self, inner: LlmPort) -> None:
        self.inner = inner
        self.turns = []
        self.resolved_model_id = getattr(inner, "model", "")

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> LlmTurn:
        turn = self.inner.complete(messages, tools)
        if turn.resolved_model_id:
            self.resolved_model_id = turn.resolved_model_id
        request_body = getattr(self.inner, "last_request_body", None)
        raw_response = getattr(self.inner, "last_raw_response", None)
        self.turns.append(
            {
                "text": turn.text,
                "reasoning": turn.reasoning,
                "input_tokens": turn.input_tokens,
                "output_tokens": turn.output_tokens,
                "resolved_model_id": turn.resolved_model_id,
                "request_body": request_body,
                "raw_response": raw_response,
                "tool_calls": [
                    {
                        "id": call.call_id,
                        "name": call.name,
                        "arguments": call.arguments,
                    }
                    for call in turn.tool_calls
                ],
            }
        )
        return turn


def persist_agent_run(
    *,
    out_dir: Path,
    proposal: AIProposal,
    turns: list[dict[str, Any]],
    history: dict[str, Any],
    meta: dict[str, Any],
    attention: tuple[AttentionRequest, ...] = (),
) -> Path:
    """Write proposal, transcript, history and a human-readable reasoning file."""
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "proposal.json").write_text(
        proposal.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    transcript = out_dir / "transcript.jsonl"
    with transcript.open("w", encoding="utf-8") as handle:
        for turn in turns:
            handle.write(json.dumps(turn, default=str) + "\n")
    (out_dir / "history.json").write_text(
        json.dumps(history, indent=2, default=str) + "\n", encoding="utf-8"
    )
    (out_dir / "meta.json").write_text(
        json.dumps(meta, indent=2, default=str) + "\n", encoding="utf-8"
    )
    if attention:
        (out_dir / "attention.json").write_text(
            json.dumps(
                [item.model_dump(mode="json") for item in attention],
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    _write_run_artifacts(out_dir, turns)
    (out_dir / "reasoning.md").write_text(
        _reasoning_markdown(proposal, turns), encoding="utf-8"
    )
    return out_dir


def _write_run_artifacts(out_dir: Path, turns: list[dict[str, Any]]) -> None:
    requests: list[dict[str, Any]] = []
    responses: list[dict[str, Any]] = []
    for index, turn in enumerate(turns, start=1):
        request_body = turn.get("request_body")
        if isinstance(request_body, dict):
            requests.append({"turn": index, "request_body": request_body})
        raw_response = turn.get("raw_response")
        if isinstance(raw_response, dict):
            responses.append({"turn": index, "raw_response": raw_response})
    if requests:
        (out_dir / "requests.json").write_text(
            json.dumps(requests, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
    if responses:
        (out_dir / "raw_responses.json").write_text(
            json.dumps(responses, indent=2, default=str) + "\n",
            encoding="utf-8",
        )


def _reasoning_markdown(proposal: AIProposal, turns: list[dict[str, Any]]) -> str:
    families = (
        "\n".join(
            f"- {action.strategy_id}: {action.stance.value}"
            for action in proposal.family_actions
        )
        or "- (none; ABSTAIN or no family actions)"
    )
    model_thoughts: list[str] = []
    for index, turn in enumerate(turns, start=1):
        reasoning = str(turn.get("reasoning") or "").strip()
        text = str(turn.get("text") or "").strip()
        calls = turn.get("tool_calls") or []
        names = []
        if isinstance(calls, list):
            names = [
                str(call.get("name"))
                for call in calls
                if isinstance(call, dict) and call.get("name")
            ]
        block = [f"## Turn {index}"]
        if names:
            block.append("Tools: " + ", ".join(names))
        if reasoning:
            block.append("### Model reasoning")
            block.append(reasoning)
        if text:
            block.append("### Assistant text")
            block.append(text)
        model_thoughts.append("\n\n".join(block))
    missing = "\n".join(f"- {item}" for item in proposal.missing_data) or "- (none)"
    return (
        f"# Layer 4 weekly agent run\n\n"
        f"Proposal `{proposal.proposal_id}`  \n"
        f"Recommendation: **{proposal.recommendation.value}**  \n"
        f"Confidence: {proposal.confidence}\n\n"
        f"## Family actions\n\n{families}\n\n"
        f"## Narrative\n\n{proposal.narrative or '(empty)'}\n\n"
        f"## Missing data\n\n{missing}\n\n"
        f"# Transcript\n\n"
        + ("\n\n".join(model_thoughts) if model_thoughts else "(no model turns)")
        + "\n"
    )
