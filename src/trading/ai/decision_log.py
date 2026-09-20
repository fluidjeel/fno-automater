"""Append-only Agent Desk decision log. Zero LLM calls; never a live mutation.

Invariant 2: recording a decision cannot place an order or mutate config.
The writer validates the contract then persists through a typed port so this
module never imports the trading store, broker or OMS.
"""

from __future__ import annotations

from typing import Protocol

from trading.domain.contracts.agent_decision import AgentDecision
from trading.domain.enums import DeskRole

__all__ = ["AgentDecisionPort", "DecisionLog"]


class AgentDecisionPort(Protocol):
    """Persist and query logged desk decisions. Implemented by TradingStore."""

    def insert_agent_decision(self, decision: AgentDecision) -> None: ...

    def get_agent_decision(self, decision_id: str) -> AgentDecision | None: ...

    def list_agent_decisions(
        self,
        *,
        role: DeskRole | None = None,
        model_id: str | None = None,
        prompt_version: str | None = None,
        policy_version: str | None = None,
    ) -> tuple[AgentDecision, ...]: ...


class DecisionLog:
    """Thin deterministic API over `agent_decisions`. No LLM, no broker calls."""

    def __init__(self, store: AgentDecisionPort) -> None:
        self._store = store

    def record(self, decision: AgentDecision) -> AgentDecision:
        """Validate then append. Duplicate decision_id fails closed."""
        verified = AgentDecision.model_validate(decision.model_dump(mode="json"))
        self._store.insert_agent_decision(verified)
        return verified

    def get(self, decision_id: str) -> AgentDecision | None:
        """Load one decision by id, or None when absent."""
        return self._store.get_agent_decision(decision_id)

    def list(
        self,
        *,
        role: DeskRole | None = None,
        model_id: str | None = None,
        prompt_version: str | None = None,
        policy_version: str | None = None,
    ) -> tuple[AgentDecision, ...]:
        """Query by role and/or (model_id, prompt_version, policy_version)."""
        return self._store.list_agent_decisions(
            role=role,
            model_id=model_id,
            prompt_version=prompt_version,
            policy_version=policy_version,
        )
