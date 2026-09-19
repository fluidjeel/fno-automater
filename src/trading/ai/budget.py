"""Token spend tracker. Exceeding the cap aborts the loop to ABSTAIN."""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal

from trading.config.agent import AgentConfig

__all__ = ["TokenBudget"]


class TokenBudget:
    """Running cost of one agent run, in INR."""

    def __init__(self, config: AgentConfig) -> None:
        self._config = config
        self.input_tokens = 0
        self.output_tokens = 0
        self.spent_inr = Decimal("0")

    def charge(self, input_tokens: int, output_tokens: int) -> bool:
        """Apply token usage. Return False when the run budget is exhausted."""
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
        if self.input_tokens + self.output_tokens > self._config.max_tokens_per_run:
            return False
        return self.spent_inr <= self._config.monthly_budget_inr
