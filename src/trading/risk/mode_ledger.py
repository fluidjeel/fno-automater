"""Mode ledgers, limits, and reservations (Phase P2 / spec §4, §10.1).

Enforces:
- Four independent ledgers on the portfolio book (default ₹7,00,000 INR).
- Reference capital = min(start allocation, conservative equity).
- No cross-mode borrowing.
- Crash recovery and reconstruction from durable store events/reservations/positions.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Final, Self

from pydantic import Field, model_validator

from trading.domain.contracts.base import StrictModel
from trading.domain.contracts.lifecycle import PositionLifecycleRecord
from trading.domain.contracts.mode_policy import ModesConfig, load_modes_config
from trading.domain.contracts.order import OrderEvent
from trading.domain.enums import ModeId, OrderState, Side, TradeState
from trading.domain.primitives import Currency, Money, Rounding
from trading.storage.trading_store import TradingEventType, TradingStore

__all__ = [
    "DEFAULT_TOTAL_EQUITY",
    "FourModeBook",
    "ModeLedger",
]

DEFAULT_TOTAL_EQUITY: Final = Money.of("700000", Currency.INR)


class ModeLedger(StrictModel):
    """Capital ledger for one mode (Invariant: no cross-mode borrow)."""

    mode_id: ModeId
    allocated_capital: Money
    realized_pnl_today: Money = Field(default_factory=lambda: Money.zero(Currency.INR))
    unrealized_pnl: Money = Field(default_factory=lambda: Money.zero(Currency.INR))
    reserved_capital: Money = Field(default_factory=lambda: Money.zero(Currency.INR))
    margin_used: Money = Field(default_factory=lambda: Money.zero(Currency.INR))
    high_water_mark: Money = Field(default_factory=lambda: Money.zero(Currency.INR))
    drawdown: Money = Field(default_factory=lambda: Money.zero(Currency.INR))

    @model_validator(mode="before")
    @classmethod
    def _populate_defaults(cls, data: Any) -> Any:
        if isinstance(data, dict):
            alloc = data.get("allocated_capital")
            curr = Currency.INR
            if isinstance(alloc, Money):
                curr = alloc.currency
            elif isinstance(alloc, dict) and "currency" in alloc:
                try:
                    curr = Currency(alloc["currency"])
                except Exception:
                    curr = Currency.INR
            zero = Money.zero(curr)
            data.setdefault("realized_pnl_today", zero)
            data.setdefault("unrealized_pnl", zero)
            data.setdefault("reserved_capital", zero)
            data.setdefault("margin_used", zero)
            data.setdefault("drawdown", zero)
            if "high_water_mark" not in data or data["high_water_mark"] is None:
                data["high_water_mark"] = alloc if alloc is not None else zero
        return data

    @model_validator(mode="after")
    def _validate_currencies(self) -> Self:
        curr = self.allocated_capital.currency
        for field_name in (
            "realized_pnl_today",
            "unrealized_pnl",
            "reserved_capital",
            "margin_used",
            "high_water_mark",
            "drawdown",
        ):
            val = getattr(self, field_name)
            if val is not None and val.currency != curr:
                raise ValueError(
                    f"{field_name} currency {val.currency} does not match "
                    f"allocated_capital currency {curr}"
                )
        return self

    @property
    def equity(self) -> Money:
        """allocated_capital + realized_pnl_today + unrealized_pnl."""
        return self.allocated_capital + self.realized_pnl_today + self.unrealized_pnl

    @property
    def conservative_equity(self) -> Money:
        """allocated_capital + realized_pnl + min(unrealized_pnl, zero)."""
        zero = Money.zero(self.allocated_capital.currency)
        penalized_unrealized = min(self.unrealized_pnl, zero)
        return self.allocated_capital + self.realized_pnl_today + penalized_unrealized

    @property
    def reference_capital(self) -> Money:
        """min(allocated_capital, conservative_equity)."""
        return min(self.allocated_capital, self.conservative_equity)

    @property
    def available_capital(self) -> Money:
        """max(equity - reserved_capital - margin_used, zero)."""
        zero = Money.zero(self.allocated_capital.currency)
        net = self.equity - self.reserved_capital - self.margin_used
        return max(net, zero)

    @property
    def free_cash(self) -> Money:
        """Property returning available_capital."""
        return self.available_capital

    def per_trade_loss_cap(self, per_trade_loss_cap_fraction: Decimal) -> Money:
        """(reference_capital * per_trade_loss_cap_fraction).quantized(FLOOR)."""
        return (self.reference_capital * per_trade_loss_cap_fraction).quantized(
            Rounding.FLOOR
        )

    def daily_loss_budget(self, daily_budget_cap_fraction: Decimal) -> Money:
        """(reference_capital * daily_budget_cap_fraction).quantized(FLOOR)."""
        return (self.reference_capital * daily_budget_cap_fraction).quantized(
            Rounding.FLOOR
        )

    def daily_loss_remaining(self, daily_budget_cap_fraction: Decimal) -> Money:
        """Remaining daily loss budget, penalized by realized loss today."""
        zero = Money.zero(self.allocated_capital.currency)
        budget = self.daily_loss_budget(daily_budget_cap_fraction)
        if self.realized_pnl_today.is_negative:
            return max(budget + self.realized_pnl_today, zero)
        return budget

    def daily_loss_breached(self, daily_budget_cap_fraction: Decimal) -> bool:
        """True if realized loss today reaches or exceeds daily loss budget."""
        budget = self.daily_loss_budget(daily_budget_cap_fraction)
        return (
            self.realized_pnl_today.is_negative and (-self.realized_pnl_today) >= budget
        )

    def can_reserve(self, amount: Money) -> bool:
        """True if requested amount <= available_capital."""
        return amount <= self.available_capital

    def record_reservation(self, amount: Money) -> ModeLedger:
        """Returns updated copy with reserved_capital + amount."""
        if amount.is_negative:
            raise ValueError("reservation amount must not be negative")
        return self.model_copy(
            update={"reserved_capital": self.reserved_capital + amount}
        )

    def release_reservation(self, amount: Money) -> ModeLedger:
        """Returns updated copy with max(reserved_capital - amount, zero)."""
        zero = Money.zero(self.allocated_capital.currency)
        new_reserved = max(self.reserved_capital - amount, zero)
        return self.model_copy(update={"reserved_capital": new_reserved})

    def record_fill(self, margin: Money, premium: Money) -> ModeLedger:
        """Record fill by updating margin_used and releasing hold."""
        zero = Money.zero(self.allocated_capital.currency)
        released_hold = max(margin + premium, zero)
        new_reserved = max(self.reserved_capital - released_hold, zero)
        new_margin = self.margin_used + margin
        return self.model_copy(
            update={
                "reserved_capital": new_reserved,
                "margin_used": new_margin,
            }
        )


class FourModeBook:
    """Four-mode capital book on the ₹7,00,000 portfolio (§4, §10.1)."""

    def __init__(
        self,
        total_equity: Money = DEFAULT_TOTAL_EQUITY,
        modes_config: ModesConfig | None = None,
        *,
        ledgers: dict[ModeId, ModeLedger] | None = None,
    ) -> None:
        self._total_equity = total_equity
        self._modes_config = modes_config or load_modes_config()
        if ledgers is not None:
            self._ledgers = dict(ledgers)
        else:
            self._ledgers = self._build_ledgers(self._total_equity, self._modes_config)

    @classmethod
    def _build_ledgers(
        cls, total_equity: Money, modes_config: ModesConfig
    ) -> dict[ModeId, ModeLedger]:
        ledgers: dict[ModeId, ModeLedger] = {}
        for mode_id in (
            ModeId.M1_CAS,
            ModeId.M2_DIRECTIONAL,
            ModeId.M3_TACTICAL_POSITIONAL,
            ModeId.M4_STRATEGIC_POSITIONAL,
        ):
            policy = modes_config.modes[mode_id]
            alloc = (total_equity * policy.capital_share).quantized(Rounding.FLOOR)
            ledgers[mode_id] = ModeLedger(
                mode_id=mode_id,
                allocated_capital=alloc,
                high_water_mark=alloc,
            )
        return ledgers

    @property
    def total_equity(self) -> Money:
        return self._total_equity

    @property
    def modes_config(self) -> ModesConfig:
        return self._modes_config

    @property
    def ledgers(self) -> dict[ModeId, ModeLedger]:
        return dict(self._ledgers)

    def get_ledger(self, mode_id: ModeId) -> ModeLedger:
        if mode_id not in self._ledgers:
            raise KeyError(f"unknown mode_id: {mode_id}")
        return self._ledgers[mode_id]

    def reference_capital_for(self, mode_id: ModeId) -> Money:
        return self.get_ledger(mode_id).reference_capital

    def available_for(self, mode_id: ModeId) -> Money:
        return self.get_ledger(mode_id).available_capital

    def try_reserve(self, mode_id: ModeId, amount: Money) -> bool:
        """Reserve amount if available in mode; return False otherwise."""
        ledger = self.get_ledger(mode_id)
        if not ledger.can_reserve(amount):
            return False
        self._ledgers[mode_id] = ledger.record_reservation(amount)
        return True

    def release_reservation(self, mode_id: ModeId, amount: Money) -> None:
        ledger = self.get_ledger(mode_id)
        self._ledgers[mode_id] = ledger.release_reservation(amount)

    @classmethod
    def reconstruct_from_store(
        cls,
        store: TradingStore,
        as_of_date: date,
        *,
        total_equity: Money | None = None,
        modes_config: ModesConfig | None = None,
    ) -> FourModeBook:
        """Restore each mode's equity, reservations, and day loss."""
        cfg = modes_config or load_modes_config()
        book_equity = total_equity or DEFAULT_TOTAL_EQUITY
        currency = book_equity.currency

        base_allocations, balances = _init_mode_balances(cfg, book_equity, currency)
        lifecycles = store.list_position_lifecycle()
        intent_to_mode = {
            r.intent.intent_id: r.intent.mode_id
            for r in lifecycles
            if r.intent.mode_id is not None
        }

        _accumulate_reservations(store, intent_to_mode, balances["reserved"])
        orders_by_id = _index_order_events(store)
        _accumulate_positions(lifecycles, orders_by_id, as_of_date, currency, balances)

        final_ledgers = _assemble_final_ledgers(base_allocations, balances, currency)
        return FourModeBook(
            total_equity=book_equity,
            modes_config=cfg,
            ledgers=final_ledgers,
        )


def _init_mode_balances(
    cfg: ModesConfig, book_equity: Money, currency: Currency
) -> tuple[dict[ModeId, Money], dict[str, dict[ModeId, Money]]]:
    mode_ids = (
        ModeId.M1_CAS,
        ModeId.M2_DIRECTIONAL,
        ModeId.M3_TACTICAL_POSITIONAL,
        ModeId.M4_STRATEGIC_POSITIONAL,
    )
    base_alloc: dict[ModeId, Money] = {}
    balances: dict[str, dict[ModeId, Money]] = {
        "reserved": {},
        "margin": {},
        "realized_today": {},
        "prior_realized": {},
        "unrealized": {},
    }
    for m in mode_ids:
        policy = cfg.modes[m]
        base_alloc[m] = (book_equity * policy.capital_share).quantized(Rounding.FLOOR)
        for cat_dict in balances.values():
            cat_dict[m] = Money.zero(currency)
    return base_alloc, balances


def _accumulate_reservations(
    store: TradingStore,
    intent_to_mode: dict[str, ModeId],
    reserved_by_mode: dict[ModeId, Money],
) -> None:
    for res in store.list_reservations():
        if not res.state.holds_capital:
            continue
        m_id = res.mode_id or intent_to_mode.get(res.intent_id)
        if m_id is not None and m_id in reserved_by_mode:
            reserved_by_mode[m_id] = reserved_by_mode[m_id] + res.amount


def _index_order_events(store: TradingStore) -> dict[str, OrderEvent]:
    orders_by_id: dict[str, OrderEvent] = {}
    for stored in store.read_events():
        if stored.event_type is TradingEventType.ORDER_EVENT:
            evt = stored.deserialize()
            if isinstance(evt, OrderEvent):
                orders_by_id[evt.identity.internal_order_id] = evt
    return orders_by_id


def _accumulate_positions(
    lifecycles: tuple[PositionLifecycleRecord, ...],
    orders_by_id: dict[str, OrderEvent],
    as_of_date: date,
    currency: Currency,
    balances: dict[str, dict[ModeId, Money]],
) -> None:
    open_states = {
        TradeState.OPEN,
        TradeState.CLOSING,
        TradeState.EXIT_PENDING,
        TradeState.REPAIR_REQUIRED,
    }
    for record in lifecycles:
        m_id = record.intent.mode_id
        if m_id is None or m_id not in balances["reserved"]:
            continue

        pos_state = record.position.state
        if pos_state in open_states:
            margin_req = record.risk_decision.margin_required
            if margin_req is not None:
                balances["margin"][m_id] = balances["margin"][m_id] + margin_req
        elif pos_state is TradeState.CLOSED:
            trade_pnl = _calculate_closed_trade_pnl(record, orders_by_id, currency)
            close_date = record.as_of.date()
            if close_date == as_of_date:
                balances["realized_today"][m_id] = (
                    balances["realized_today"][m_id] + trade_pnl
                )
            elif close_date < as_of_date:
                balances["prior_realized"][m_id] = (
                    balances["prior_realized"][m_id] + trade_pnl
                )


def _assemble_final_ledgers(
    base_alloc: dict[ModeId, Money],
    balances: dict[str, dict[ModeId, Money]],
    currency: Currency,
) -> dict[ModeId, ModeLedger]:
    final_ledgers: dict[ModeId, ModeLedger] = {}
    for m, alloc_base in base_alloc.items():
        start_alloc = alloc_base + balances["prior_realized"][m]
        realized_today = balances["realized_today"][m]
        reserved = balances["reserved"][m]
        margin = balances["margin"][m]
        unrealized = balances["unrealized"][m]
        equity = start_alloc + realized_today + unrealized
        hwm = max(start_alloc, equity)
        drawdown = max(hwm - equity, Money.zero(currency))
        final_ledgers[m] = ModeLedger(
            mode_id=m,
            allocated_capital=start_alloc,
            realized_pnl_today=realized_today,
            unrealized_pnl=unrealized,
            reserved_capital=reserved,
            margin_used=margin,
            high_water_mark=hwm,
            drawdown=drawdown,
        )
    return final_ledgers


def _calculate_closed_trade_pnl(
    record: PositionLifecycleRecord,
    orders_by_id: dict[str, OrderEvent],
    currency: Currency,
) -> Money:
    """Calculate net cash flow P&L for a closed position across legs and exit fills."""
    net_pnl_decimal = Decimal(0)
    for leg in record.position.legs:
        entry_val = leg.average_entry_price.value * Decimal(leg.quantity_contracts)
        if leg.side is Side.BUY:
            net_pnl_decimal -= entry_val
        else:
            net_pnl_decimal += entry_val

    has_exit_fills = False
    for order_id in record.exit_order_ids:
        order = orders_by_id.get(order_id)
        if (
            order is not None
            and order.state in {OrderState.FILLED, OrderState.PARTIAL}
            and order.average_fill_price is not None
        ):
            has_exit_fills = True
            fill_val = order.average_fill_price.value * Decimal(order.filled_quantity)
            if order.command.side is Side.SELL:
                net_pnl_decimal += fill_val
            else:
                net_pnl_decimal -= fill_val

    if not has_exit_fills:
        return Money.zero(currency)

    return Money.of(str(net_pnl_decimal), currency).quantized(Rounding.HALF_EVEN)
