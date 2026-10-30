"""LLM overlay and fallback for platter agent."""

from __future__ import annotations

from trading.ai.platter_agent.llm_overlay import decide_agent_direction, resolve_llm_enabled
from trading.ai.ports import LlmTimeoutError
from trading.data.platter_pipeline.config import load_platter_pipeline_config


class _FailingLlm:
    def complete_text(self, *args: object, **kwargs: object) -> str:
        raise LlmTimeoutError("LLM HTTP call timed out")


def _minimal_platter(*, fail_closed: bool = False) -> dict:
    return {
        "fail_closed": fail_closed,
        "events": {"blackout_active": False},
        "mechanical": {"setup_direction": "UP"},
        "flow": {"session_delta": 1000},
        "depth": {"imbalance_l5": "0.1"},
    }


def test_llm_timeout_falls_back_to_rubric() -> None:
    config = load_platter_pipeline_config()
    platter = _minimal_platter()
    rubric_only = decide_agent_direction(
        platter, config, llm_enabled=False, llm_client=_FailingLlm()
    )
    decision = decide_agent_direction(
        platter, config, llm_enabled=True, llm_client=_FailingLlm()
    )
    assert decision.llm_used is False
    assert decision.llm_failure_reason is not None
    assert decision.direction == rubric_only.direction
    assert any("llm fallback" in reason for reason in decision.reasons)


def test_resolve_llm_enabled_cli_overrides_config(monkeypatch: object) -> None:
    config = load_platter_pipeline_config()
    assert resolve_llm_enabled(config, cli_override=False) is False
    assert resolve_llm_enabled(config, cli_override=True) is True
