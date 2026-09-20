"""Token spend tracker. Exceeding the cap aborts the loop to ABSTAIN."""

from __future__ import annotations

from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal

from trading.ai.budget_port import AgentBudgetPort
from trading.config.agent import AgentConfig
from trading.domain.clock import Clock

__all__ = ["TokenBudget"]


class TokenBudget:
    """Running cost of one agent run, in INR, optionally persisted monthly."""

    def __init__(
        self,
        config: AgentConfig,
        *,
        role: str = "weekly",
        store: AgentBudgetPort | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._config = config
        self._role = role
        self._store = store
        self._clock = clock
        self.input_tokens = 0
        self.output_tokens = 0
        self.spent_inr = Decimal("0")
        self._prior_spent_inr = Decimal("0")
        self._prior_input_tokens = 0
        self._prior_output_tokens = 0
        if store is not None and clock is not None:
            year_month = _year_month(clock.now_utc())
            snapshot = store.get_agent_budget(year_month, role)
            self._prior_spent_inr = snapshot.spent_inr
            self._prior_input_tokens = snapshot.input_tokens
            self._prior_output_tokens = snapshot.output_tokens

    @property
    def total_spent_inr(self) -> Decimal:
        """Persisted spend for the month plus this run."""
        return self._prior_spent_inr + self.spent_inr

    @property
    def total_input_tokens(self) -> int:
        return self._prior_input_tokens + self.input_tokens

    @property
    def total_output_tokens(self) -> int:
        return self._prior_output_tokens + self.output_tokens

    def charge(self, input_tokens: int, output_tokens: int) -> bool:
        """Apply token usage. Return False when run or monthly budget is exhausted."""
        self.input_tokens += max(input_tokens, 0)
        self.output_tokens += max(output_tokens, 0)
        million = Decimal("1000000")
        added = (
            Decimal(max(input_tokens, 0))
            / million
            * self._config.input_inr_per_million_tokens
            + Decimal(max(output_tokens, 0))
            / million
            * self._config.output_inr_per_million_tokens
        )
        self.spent_inr = (self.spent_inr + added).quantize(
            Decimal("0.0001"), rounding=ROUND_HALF_EVEN
        )
        if self._store is not None and self._clock is not None:
            self._store.upsert_agent_budget(
                _year_month(self._clock.now_utc()),
                self._role,
                spent_inr=self.total_spent_inr,
                input_tokens=self.total_input_tokens,
                output_tokens=self.total_output_tokens,
                updated_at=self._clock.now_utc(),
            )
        if self.input_tokens + self.output_tokens > self._config.max_tokens_per_run:
            return False
        return self.total_spent_inr <= self._config.monthly_budget_inr


def _year_month(value: datetime) -> str:
    if not isinstance(value, datetime):
        raise TypeError("clock must return timezone-aware datetimes")
    return value.strftime("%Y-%m")
