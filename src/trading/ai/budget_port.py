"""Port for persisted monthly agent budgets. No storage imports in callers."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Protocol

from trading.domain.contracts.agent_budget import AgentBudgetSnapshot

__all__ = ["AgentBudgetPort", "AgentBudgetSnapshot"]


class AgentBudgetPort(Protocol):
    """Load and persist monthly INR/token spend per agent role."""

    def get_agent_budget(self, year_month: str, role: str) -> AgentBudgetSnapshot: ...

    def upsert_agent_budget(
        self,
        year_month: str,
        role: str,
        *,
        spent_inr: Decimal,
        input_tokens: int,
        output_tokens: int,
        updated_at: datetime | None = None,
    ) -> None: ...
