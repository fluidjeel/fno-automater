"""Campaign accounting across linked position replacements (P12 / T56)."""

from __future__ import annotations

from typing import Any

from pydantic import model_validator

from trading.domain.contracts.base import NonEmptyStr, StrictBool, VersionedModel
from trading.domain.enums import ModeId
from trading.domain.primitives import Currency, Money

__all__ = ["CampaignRecord"]


class CampaignRecord(VersionedModel):
    """Cumulative realized outcome for one campaign across trade ids."""

    campaign_id: NonEmptyStr
    mode_id: ModeId
    trade_ids: tuple[NonEmptyStr, ...]
    recorded_closes: tuple[NonEmptyStr, ...] = ()
    cumulative_realized_gross: Money
    cumulative_charges: Money
    cumulative_realized_net: Money
    cumulative_estimated_net: Money
    high_water_mark_net: Money
    drawdown: Money
    loss_limit: Money
    entries_blocked: StrictBool = False

    @model_validator(mode="before")
    @classmethod
    def _default_estimated_net(cls, data: Any) -> Any:
        if isinstance(data, dict) and "cumulative_estimated_net" not in data:
            gross = data.get("cumulative_realized_net")
            if gross is not None:
                data["cumulative_estimated_net"] = gross
            else:
                data["cumulative_estimated_net"] = Money.zero(Currency.INR)
        return data
