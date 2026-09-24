"""Campaign drawdown ledger: cumulative realized P&L survives position id changes (P12)."""

from __future__ import annotations

from dataclasses import dataclass, field

from trading.domain.primitives import Currency, Money, Rounding

__all__ = ["CampaignDrawdownLedger", "CampaignDrawdownRecord"]


@dataclass(frozen=True, slots=True)
class CampaignDrawdownRecord:
    """Cumulative realized outcome for one campaign across position ids."""

    campaign_id: str
    cumulative_realized_pnl: Money
    position_ids: tuple[str, ...]


@dataclass
class CampaignDrawdownLedger:
    """Tracks campaign-level drawdown across linked position replacements."""

    _records: dict[str, CampaignDrawdownRecord] = field(default_factory=dict)

    def record_realized_pnl(
        self,
        *,
        campaign_id: str,
        position_id: str,
        realized_pnl: Money,
    ) -> CampaignDrawdownRecord:
        """Append realized P&L for a campaign; a new position id does not reset the ledger."""
        existing = self._records.get(campaign_id)
        if existing is None:
            updated = CampaignDrawdownRecord(
                campaign_id=campaign_id,
                cumulative_realized_pnl=realized_pnl,
                position_ids=(position_id,),
            )
        else:
            cumulative = (existing.cumulative_realized_pnl + realized_pnl).quantized(
                Rounding.HALF_EVEN
            )
            updated = CampaignDrawdownRecord(
                campaign_id=campaign_id,
                cumulative_realized_pnl=cumulative,
                position_ids=(*existing.position_ids, position_id),
            )
        self._records[campaign_id] = updated
        return updated

    def cumulative_realized_pnl(self, campaign_id: str) -> Money:
        record = self._records.get(campaign_id)
        if record is None:
            return Money.zero(Currency.INR)
        return record.cumulative_realized_pnl

    def get(self, campaign_id: str) -> CampaignDrawdownRecord | None:
        return self._records.get(campaign_id)
