"""Isolated counterfactual mode books that never touch reservations (P12 / §4.3, T49)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from trading.domain.contracts.intent import TradeIntent
from trading.domain.enums import ModeId
from trading.portfolio.arbitration import ArbitrationResult

__all__ = ["CounterfactualModeBook", "CounterfactualModeBookEntry"]


@dataclass(frozen=True, slots=True)
class CounterfactualModeBookEntry:
    """One hypothetical allocation row in an isolated mode book."""

    book_id: str
    mode_id: ModeId | None
    candidate_intent_id: str
    family_id: str | None
    action: str
    incumbent_id: str | None
    requested_risk_amount: Decimal
    timestamp: datetime


@dataclass
class CounterfactualModeBook:
    """Append-only shadow book for M3/M4 policy comparison without capital mutation."""

    book_id: str
    mode_id: ModeId
    _entries: list[CounterfactualModeBookEntry] = field(default_factory=list)

    def ingest_arbitration(self, result: ArbitrationResult, *, now: datetime) -> None:
        """Record arbitration outcomes for this mode's candidates only."""
        for entry in result.counterfactual_log:
            if entry.mode_id is not self.mode_id:
                continue
            self._entries.append(
                CounterfactualModeBookEntry(
                    book_id=self.book_id,
                    mode_id=entry.mode_id,
                    candidate_intent_id=entry.candidate_intent_id,
                    family_id=entry.family_id.value if entry.family_id else None,
                    action=entry.action,
                    incumbent_id=entry.incumbent_id,
                    requested_risk_amount=entry.requested_risk_amount,
                    timestamp=now,
                )
            )

    def record_suppressed_candidate(
        self,
        intent: TradeIntent,
        *,
        action: str,
        incumbent_id: str | None,
        now: datetime,
    ) -> None:
        """Append a separately labelled counterfactual observation."""
        risk_amt = intent.requested_risk.amount
        self._entries.append(
            CounterfactualModeBookEntry(
                book_id=self.book_id,
                mode_id=intent.mode_id,
                candidate_intent_id=intent.intent_id,
                family_id=intent.family_id,
                action=action,
                incumbent_id=incumbent_id,
                requested_risk_amount=risk_amt,
                timestamp=now,
            )
        )

    @property
    def entries(self) -> tuple[CounterfactualModeBookEntry, ...]:
        return tuple(self._entries)

    def total_hypothetical_risk(self) -> Decimal:
        return sum(
            (
                entry.requested_risk_amount
                for entry in self._entries
                if entry.action == "APPROVED"
            ),
            Decimal("0"),
        )


def build_mode_books(
    modes: Sequence[ModeId],
    *,
    prefix: str = "cf",
) -> dict[ModeId, CounterfactualModeBook]:
    """Create one isolated counterfactual book per mode."""
    return {
        mode: CounterfactualModeBook(book_id=f"{prefix}-{mode.value}", mode_id=mode)
        for mode in modes
    }
