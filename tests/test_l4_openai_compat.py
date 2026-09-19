"""OpenAI-compatible parser and recording persist. Invariants 2 and 22."""

from __future__ import annotations

from pathlib import Path

import tests.factories as f
from trading.ai.history_ports import history_evidence
from trading.ai.openai_compat import openai_tool_specs, parse_openai_chat_completion
from trading.ai.recording import persist_agent_run
from trading.ai.tools import TOOL_SPECS
from trading.domain.clock import FrozenClock
from trading.domain.enums import Recommendation


def test_parse_openai_tool_call_and_reasoning() -> None:
    turn = parse_openai_chat_completion(
        {
            "choices": [
                {
                    "message": {
                        "content": "checking scorecard",
                        "reasoning_content": "eligibility is fail-closed",
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "function": {
                                    "name": "read_eligibility",
                                    "arguments": "{}",
                                },
                            }
                        ],
                    }
                }
            ],
            "usage": {"prompt_tokens": 11, "completion_tokens": 7},
        }
    )
    assert turn.text == "checking scorecard"
    assert turn.reasoning == "eligibility is fail-closed"
    assert turn.tool_calls[0].name == "read_eligibility"
    assert turn.input_tokens == 11


def test_openai_tool_specs_wrap_internal_schema() -> None:
    wrapped = openai_tool_specs(TOOL_SPECS)
    names = {item["function"]["name"] for item in wrapped}
    assert "emit_proposal" in names
    assert wrapped[0]["type"] == "function"


def test_persist_agent_run_writes_reasoning(tmp_path: Path) -> None:
    proposal = f.proposal(recommendation=Recommendation.ABSTAIN, family_actions=())
    out = persist_agent_run(
        out_dir=tmp_path / "run",
        proposal=proposal,
        turns=[
            {
                "text": "will abstain",
                "reasoning": "charges unverified",
                "tool_calls": [{"id": "1", "name": "emit_proposal", "arguments": {}}],
            }
        ],
        history={"bar_count": 2},
        meta={"model": "deepseek-chat"},
    )
    reasoning = (out / "reasoning.md").read_text(encoding="utf-8")
    assert "charges unverified" in reasoning
    assert "will abstain" in reasoning
    assert (out / "proposal.json").is_file()
    assert (out / "transcript.jsonl").is_file()


def test_history_evidence_is_allowlisted() -> None:
    clock = FrozenClock(f.NOW)
    payload = history_evidence(
        symbol="NSE:NIFTY50-INDEX",
        resolution="D",
        bars=[{"timestamp": 1, "close": "25000"}],
        published_at=f.NOW,
        clock=clock,
    )
    template = payload["evidence_template"]
    assert template["allowlisted"] is True
    assert template["source_id"] == "fyers-history"
    assert template["content_hash"]
