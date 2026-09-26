"""Campaign ledger: cumulative realized P&L survives position id changes (P12)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from trading.domain.contracts.campaign import CampaignRecord
from trading.domain.contracts.fill_charges import FillChargeRecord
from trading.domain.contracts.lifecycle import PositionLifecycleRecord
from trading.domain.contracts.mode_policy import ModesConfig, load_modes_config
from trading.domain.contracts.order import OrderEvent
from trading.domain.enums import ModeId, OrderState
from trading.domain.primitives import Currency, Money, Rounding
from trading.portfolio.conservative_net import (
    confirmed_net,
    conservative_realized_net,
    estimated_net,
)
from trading.portfolio.fill_ledger import (
    trade_confirmed_charges,
    trade_fill_cash_flow,
)
from trading.storage.trading_store import TradingStore

if TYPE_CHECKING:
    from trading.risk.mode_ledger import FourModeBook

__all__ = [
    "CampaignDrawdownLedger",
    "CampaignDrawdownRecord",
    "CampaignLedger",
    "TradeAccounting",
    "campaign_loss_limit",
    "conservative_campaign_net",
    "estimate_trade_charges",
    "realized_gross_for_trade",
    "trade_accounting",
]


@dataclass(frozen=True, slots=True)
class TradeAccounting:
    """Gross, durable charges and model estimate for one closed trade."""

    realized_gross: Money
    confirmed_charges: Money
    estimated_charges: Money
    realized_net: Money
    estimated_net: Money
    conservative_net: Money


@dataclass(frozen=True, slots=True)
class CampaignDrawdownRecord:
    """Legacy cumulative realized outcome view."""

    campaign_id: str
    cumulative_realized_pnl: Money
    position_ids: tuple[str, ...]


@dataclass
class CampaignDrawdownLedger:
    """In-memory campaign tracker (superseded by :class:`CampaignLedger`)."""

    _records: dict[str, CampaignDrawdownRecord] = field(default_factory=dict)

    def record_realized_pnl(
        self,
        *,
        campaign_id: str,
        position_id: str,
        realized_pnl: Money,
    ) -> CampaignDrawdownRecord:
        """Append realized P&L for a campaign; a new position id does not reset."""
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


@dataclass
class CampaignLedger:
    """Durable campaign accounting across roll/switch replacement trade ids."""

    _records: dict[str, CampaignRecord] = field(default_factory=dict)

    def get(self, campaign_id: str) -> CampaignRecord | None:
        return self._records.get(campaign_id)

    def list_records(self) -> tuple[CampaignRecord, ...]:
        return tuple(self._records.values())

    def campaigns_blocking_entries(self) -> tuple[str, ...]:
        return tuple(
            record.campaign_id
            for record in self._records.values()
            if record.entries_blocked
        )

    def begin_campaign(
        self,
        *,
        campaign_id: str,
        mode_id: ModeId,
        loss_limit: Money,
        trade_id: str,
    ) -> CampaignRecord:
        """Open a campaign for the first trade in a roll chain."""
        currency = loss_limit.currency
        zero = Money.zero(currency)
        record = CampaignRecord(
            campaign_id=campaign_id,
            mode_id=mode_id,
            trade_ids=(trade_id,),
            cumulative_realized_gross=zero,
            cumulative_charges=zero,
            cumulative_realized_net=zero,
            cumulative_estimated_net=zero,
            high_water_mark_net=zero,
            drawdown=zero,
            loss_limit=loss_limit,
            entries_blocked=False,
        )
        self._records[campaign_id] = record
        return record

    def link_trade(self, *, campaign_id: str, trade_id: str) -> CampaignRecord:
        """Attach a replacement trade id without resetting campaign P&L."""
        record = self._require(campaign_id)
        if trade_id in record.trade_ids:
            return record
        updated = record.model_copy(update={"trade_ids": (*record.trade_ids, trade_id)})
        self._records[campaign_id] = updated
        return updated

    def record_close(
        self,
        *,
        campaign_id: str,
        trade_id: str,
        accounting: TradeAccounting,
    ) -> CampaignRecord:
        """Append one closed trade; duplicate closes are idempotent."""
        record = self._require(campaign_id)
        if trade_id in record.recorded_closes:
            return record
        currency = record.cumulative_realized_gross.currency
        gross = (
            record.cumulative_realized_gross + accounting.realized_gross
        ).quantized(Rounding.HALF_EVEN)
        total_charges = (
            record.cumulative_charges + accounting.confirmed_charges
        ).quantized(Rounding.HALF_EVEN)
        net = confirmed_net(gross, total_charges)
        cumulative_estimated = (
            record.cumulative_estimated_net + accounting.estimated_net
        ).quantized(Rounding.HALF_EVEN)
        enforcement_net = conservative_campaign_net(
            CampaignRecord(
                campaign_id=record.campaign_id,
                mode_id=record.mode_id,
                trade_ids=record.trade_ids,
                recorded_closes=record.recorded_closes,
                cumulative_realized_gross=gross,
                cumulative_charges=total_charges,
                cumulative_realized_net=net,
                cumulative_estimated_net=cumulative_estimated,
                high_water_mark_net=record.high_water_mark_net,
                drawdown=record.drawdown,
                loss_limit=record.loss_limit,
                entries_blocked=record.entries_blocked,
            )
        )
        hwm = max(record.high_water_mark_net, net)
        drawdown = max(hwm - enforcement_net, Money.zero(currency))
        entries_blocked = self._loss_limit_breached(
            net=enforcement_net,
            loss_limit=record.loss_limit,
            additional_risk=Money.zero(currency),
        )
        updated = record.model_copy(
            update={
                "recorded_closes": (*record.recorded_closes, trade_id),
                "cumulative_realized_gross": gross,
                "cumulative_charges": total_charges,
                "cumulative_realized_net": net,
                "cumulative_estimated_net": cumulative_estimated,
                "high_water_mark_net": hwm,
                "drawdown": drawdown,
                "entries_blocked": entries_blocked or record.entries_blocked,
            }
        )
        self._records[campaign_id] = updated
        return updated

    def projected_loss_limit_breached(
        self,
        *,
        campaign_id: str,
        additional_risk: Money,
    ) -> bool:
        """Return whether ``additional_risk`` would breach the campaign loss cap."""
        record = self._require(campaign_id)
        if record.entries_blocked:
            return True
        return self._loss_limit_breached(
            net=conservative_campaign_net(record),
            loss_limit=record.loss_limit,
            additional_risk=additional_risk,
        )

    def load_record(self, record: CampaignRecord) -> None:
        """Replace one campaign snapshot (restart recovery)."""
        self._records[record.campaign_id] = record

    @classmethod
    def reconstruct_from_store(cls, store: TradingStore) -> CampaignLedger:
        ledger = cls()
        for record in store.list_campaign_records():
            ledger.load_record(record)
        return ledger

    def _require(self, campaign_id: str) -> CampaignRecord:
        record = self._records.get(campaign_id)
        if record is None:
            raise KeyError(f"unknown campaign: {campaign_id}")
        return record

    @staticmethod
    def _loss_limit_breached(
        *,
        net: Money,
        loss_limit: Money,
        additional_risk: Money,
    ) -> bool:
        zero = Money.zero(net.currency)
        worst_net = net - additional_risk
        return worst_net < (zero - loss_limit)


def campaign_loss_limit(
    mode_id: ModeId,
    *,
    mode_book: FourModeBook,
    modes_config: ModesConfig | None = None,
) -> Money:
    """Mode-scoped cumulative loss budget for one roll campaign."""
    cfg = modes_config or load_modes_config()
    policy = cfg.modes[mode_id]
    ledger = mode_book.get_ledger(mode_id)
    return (ledger.reference_capital * policy.max_open_loss_cap_fraction).quantized(
        Rounding.FLOOR
    )


def conservative_campaign_net(record: CampaignRecord) -> Money:
    """Campaign enforcement net: min(confirmed, estimated) after gross."""
    return min(record.cumulative_realized_net, record.cumulative_estimated_net)


def trade_accounting(
    record: PositionLifecycleRecord,
    orders_by_id: dict[str, OrderEvent],
    charges_by_key: dict[str, FillChargeRecord],
    *,
    charges_per_lot: Money,
) -> TradeAccounting:
    """Reconstruct gross, durable charges and model estimate for one trade."""
    loss = record.risk_decision.recalculated_max_loss
    currency = loss.currency if loss is not None else Currency.INR
    gross = trade_fill_cash_flow(record.trade_id, orders_by_id, currency)
    confirmed = trade_confirmed_charges(
        record.trade_id, orders_by_id, charges_by_key, currency
    )
    estimated_charges = estimate_trade_charges(
        record, orders_by_id, charges_per_lot=charges_per_lot
    )
    return TradeAccounting(
        realized_gross=gross,
        confirmed_charges=confirmed,
        estimated_charges=estimated_charges,
        realized_net=confirmed_net(gross, confirmed),
        estimated_net=estimated_net(gross, estimated_charges),
        conservative_net=conservative_realized_net(
            gross,
            confirmed_charges=confirmed,
            estimated_charges=estimated_charges,
        ),
    )


def estimate_trade_charges(
    record: PositionLifecycleRecord,
    orders_by_id: dict[str, OrderEvent],
    *,
    charges_per_lot: Money,
) -> Money:
    """Model-derived round-trip charges; not broker-confirmed."""
    currency = charges_per_lot.currency
    approved = record.risk_decision.approved_legs
    if not approved:
        return Money.zero(currency)
    lots = approved[0].lots.count
    fill_count = sum(
        1
        for order in orders_by_id.values()
        if order.identity.trade_id == record.trade_id
        and order.state in {OrderState.FILLED, OrderState.PARTIAL}
        and order.filled_quantity > 0
    )
    if fill_count == 0:
        return Money.zero(currency)
    return (charges_per_lot * lots * fill_count).quantized(Rounding.CEILING)


def realized_gross_for_trade(
    record: PositionLifecycleRecord,
    orders_by_id: dict[str, OrderEvent],
) -> Money:
    """Signed fill-ledger gross for one lifecycle record."""
    loss = record.risk_decision.recalculated_max_loss
    currency = loss.currency if loss is not None else Currency.INR
    return trade_fill_cash_flow(record.trade_id, orders_by_id, currency)
