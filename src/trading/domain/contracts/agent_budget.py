"""Persisted monthly Layer 4 agent token spend."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

__all__ = ["AgentBudgetSnapshot"]


@dataclass(frozen=True, slots=True)
class AgentBudgetSnapshot:
    """Monthly token spend for one agent role."""

    year_month: str
    role: str
    spent_inr: Decimal
    input_tokens: int
    output_tokens: int
    updated_at: datetime
