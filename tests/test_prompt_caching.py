"""Tests for Agent Desk Prompt Caching & Token Optimization."""

from __future__ import annotations

from trading.ai.cache_prefix import PromptCacheManager
from trading.ai.openai_compat import parse_openai_chat_completion


def test_prompt_cache_manager_byte_identical_prefix() -> None:
    manager = PromptCacheManager()
    schema = {"risk_limit": "Money", "allowed_sides": ["BUY", "SELL"]}

    # Call 1 with user query A
    messages_1 = manager.build_messages(
        dynamic_user_content="Assess market sentiment for 2026-09-20T10:00:00Z",
        domain_schemas=schema,
    )

    # Call 2 with completely different user query B
    messages_2 = manager.build_messages(
        dynamic_user_content="Evaluate weekly counterfactuals for 2026-09-27",
        domain_schemas=schema,
    )

    system_1 = messages_1[0]["content"]
    system_2 = messages_2[0]["content"]

    # Invariants for prompt caching:
    # 1. System messages must be byte-for-byte identical
    assert system_1 == system_2
    # 2. Fingerprints must be equal
    assert manager.fingerprint(system_1) == manager.fingerprint(system_2)
    # 3. Dynamic content only in user turn
    assert messages_1[1]["content"] != messages_2[1]["content"]


def test_prompt_cache_metrics_savings_calculation() -> None:
    manager = PromptCacheManager()
    # 10,000 prompt tokens with 8,000 cached (80% hit ratio)
    metrics = manager.evaluate_metrics(turn_input_tokens=10000, cached_tokens=8000)

    assert metrics.total_prompt_tokens == 10000
    assert metrics.cached_tokens == 8000
    assert metrics.uncached_tokens == 2000
    assert metrics.hit_ratio == 0.8
    # 8000 * 0.9 / 10000 * 100 = 72.0% cost discount
    assert abs(metrics.estimated_savings_pct - 72.0) < 1e-6


def test_openai_compat_parses_deepseek_cache_tokens() -> None:
    # DeepSeek native cache tokens in usage
    payload = {
        "choices": [{"message": {"content": "Market bias is NEUTRAL."}}],
        "model": "deepseek-chat",
        "usage": {
            "prompt_tokens": 1200,
            "completion_tokens": 50,
            "total_tokens": 1250,
            "prompt_cache_hit_tokens": 1050,
            "prompt_cache_miss_tokens": 150,
        },
    }

    turn = parse_openai_chat_completion(payload)
    assert turn.input_tokens == 1200
    assert turn.output_tokens == 50
    assert turn.prompt_cache_hit_tokens == 1050
    assert turn.prompt_cache_miss_tokens == 150


def test_openai_compat_parses_openai_cached_tokens() -> None:
    # OpenAI prompt_tokens_details.cached_tokens format
    payload = {
        "choices": [{"message": {"content": "Regime assessment: CALM."}}],
        "model": "gpt-4o-mini",
        "usage": {
            "prompt_tokens": 2000,
            "completion_tokens": 100,
            "prompt_tokens_details": {
                "cached_tokens": 1800,
            },
        },
    }

    turn = parse_openai_chat_completion(payload)
    assert turn.input_tokens == 2000
    assert turn.output_tokens == 100
    assert turn.prompt_cache_hit_tokens == 1800
    assert turn.prompt_cache_miss_tokens == 200
