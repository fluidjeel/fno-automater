"""Campaign accounting across linked position replacements (P12 / T56)."""

from __future__ import annotations

from trading.domain.contracts.base import NonEmptyStr, StrictBool, StrictModel
from trading.domain.enums import ModeId
from trading.domain.primitives import Money

__all__ = ["CampaignRecord"]


class CampaignRecord(StrictModel):
    """Cumulative realized outcome for one campaign across trade ids."""

    campaign_id: NonEmptyStr
    mode_id: ModeId
    trade_ids: tuple[NonEmptyStr, ...]
    recorded_closes: tuple[NonEmptyStr, ...] = ()
    cumulative_realized_gross: Money
    cumulative_charges: Money
    cumulative_realized_net: Money
    high_water_mark_net: Money
    drawdown: Money
    loss_limit: Money
    entries_blocked: StrictBool = False
