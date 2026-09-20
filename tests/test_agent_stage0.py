"""Agent Desk Stage 0: budget persistence, model lineage, agent Brier."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

import tests.factories as f
from trading.ai.budget import TokenBudget
from trading.ai.openai_compat import parse_openai_chat_completion
from trading.analytics.judgment import evaluate_judgment
from trading.config import load_evaluation_config
from trading.config.agent import AgentConfig
from trading.domain.clock import FrozenClock
from trading.storage.trading_store import TradingStore

ROOT = Path(__file__).resolve().parent.parent
EVALUATION = load_evaluation_config(ROOT / "config" / "evaluation.yaml")
NOW = datetime(2026, 9, 20, 6, 0, tzinfo=UTC)


def _agent_config(**overrides: object) -> AgentConfig:
    return AgentConfig.model_validate(
        {
            "schema_version": "1",
            "policy_version": "1",
            "enabled": True,
            "model": "deepseek-chat",
            "max_iterations": 4,
            "max_tokens_per_run": 100000,
            "monthly_budget_inr": "1",
            "input_inr_per_million_tokens": "15",
            "output_inr_per_million_tokens": "45",
            **overrides,
        }
    )


@pytest.fixture
def budget_store(tmp_path: Path) -> Iterator[TradingStore]:
    store = TradingStore.open(tmp_path / "budget.sqlite", clock=FrozenClock(NOW))
    yield store
    store.close()


def test_token_budget_shares_monthly_cap_across_runs(
    budget_store: TradingStore,
) -> None:
    """Two runs share one monthly ledger row; exhaustion aborts the second run."""
    clock = FrozenClock(NOW)
    config = _agent_config(monthly_budget_inr="1")
    first = TokenBudget(config, role="weekly", store=budget_store, clock=clock)
    assert first.charge(10000, 10000) is True
    second = TokenBudget(config, role="weekly", store=budget_store, clock=clock)
    assert second.total_spent_inr == first.total_spent_inr
    assert second.charge(10000, 10000) is False


def test_parse_openai_chat_completion_reads_resolved_model() -> None:
    turn = parse_openai_chat_completion(
        {
            "model": "deepseek-chat-20250301",
            "choices": [
                {
                    "message": {
                        "content": "ok",
                        "tool_calls": [],
                    }
                }
            ],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2},
        }
    )
    assert turn.resolved_model_id == "deepseek-chat-20250301"


def test_judgment_scores_agent_brier_separately() -> None:
    package = f.long_option_cohort_package()
    signals = list(package.signals)
    updated = []
    for index, signal in enumerate(signals):
        payload = signal.model_dump()
        if index == 0 and not signal.declined:
            payload["agent_confidence"] = Decimal("0.9")
        updated.append(type(signal).model_validate(payload))
    package = package.model_copy(update={"signals": tuple(updated)})
    report = evaluate_judgment(
        package,
        EVALUATION.config.fill_model,
        EVALUATION.config.judgment,
        as_of=package.observation_end,
    )
    assert report.brier_score is None
    assert report.agent_brier_score is not None
    assert report.agent_brier_reliability is not None
