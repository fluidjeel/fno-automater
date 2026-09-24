"""Mode ledgers, limits, and reservations (Phase P2 / spec §4, §10.1).

Enforces:
- Four independent ledgers on the portfolio book (default ₹7,00,000 INR).
- Reference capital = min(start allocation, conservative equity).
- No cross-mode borrowing.
- Crash recovery and reconstruction from durable store events/reservations/positions.

Open risk is ``reserved_capital + margin_used``. Pending entry holds live in
``reserved_capital`` (durable ``RESERVED`` only). Filled positions live in
``margin_used``. Committed reservations are not added again as reserved — they
are represented by the open position's margin so totals never double-count.

Accounting caveat (unresolved): ``realized_pnl_today`` equals
``realized_gross_pnl_today`` until broker charges are persisted on fill events.
Do not treat that equality as complete net accounting.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from threading import Lock
from typing import Any, Final, Self

from pydantic import Field, model_validator

from trading.domain.contracts.base import StrictModel
from trading.domain.contracts.lifecycle import PositionLifecycleRecord
from trading.domain.contracts.mode_policy import ModesConfig, load_modes_config
from trading.domain.contracts.order import OrderEvent
from trading.domain.enums import ModeId, OrderState, ReservationState, Side, TradeState
from trading.domain.primitives import Currency, Money, Rounding
from trading.storage.trading_store import TradingEventType, TradingStore

__all__ = [
    "DEFAULT_TOTAL_EQUITY",
    "FourModeBook",
    "ModeLedger",
]

DEFAULT_TOTAL_EQUITY: Final = Money.of("700000", Currency.INR)


class ModeLedger(StrictModel):
    """Capital ledger for one mode (Invariant: no cross-mode borrow).

    ``realized_gross_pnl_today`` is signed fill-ledger cash flow before broker
    charges. ``realized_pnl_today`` is net realized; it equals gross until
    order events carry charge fields.
    """

    mode_id: ModeId
    allocated_capital: Money
    realized_gross_pnl_today: Money = Field(
        default_factory=lambda: Money.zero(Currency.INR)
    )
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
            data.setdefault("realized_gross_pnl_today", zero)
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
            "realized_gross_pnl_today",
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
        """Remaining daily loss budget, penalized by net realized loss today."""
        zero = Money.zero(self.allocated_capital.currency)
        budget = self.daily_loss_budget(daily_budget_cap_fraction)
        loss_today = self.realized_pnl_today
        if loss_today.is_negative:
            return max(budget + loss_today, zero)
        return budget

    def daily_loss_breached(self, daily_budget_cap_fraction: Decimal) -> bool:
        """True if net realized loss today reaches or exceeds daily loss budget."""
        budget = self.daily_loss_budget(daily_budget_cap_fraction)
        loss_today = self.realized_pnl_today
        return loss_today.is_negative and (-loss_today) >= budget

    def can_reserve(self, amount: Money) -> bool:
        """True if requested amount <= available_capital."""
        return amount <= self.available_capital

    @property
    def open_risk(self) -> Money:
        """Capital held as open loss: pending reservations plus filled margin."""
        return self.reserved_capital + self.margin_used

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
        self._reserve_lock = Lock()

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

    def try_reserve(
        self,
        mode_id: ModeId,
        amount: Money,
        *,
        global_cap: Money | None = None,
    ) -> bool:
        """Reserve amount if available in mode (and under the global cap).

        Locked so concurrent proposals in one process cannot oversubscribe a
        mode or the global envelope between the check and the write.
        """
        with self._reserve_lock:
            ledger = self.get_ledger(mode_id)
            if not ledger.can_reserve(amount):
                return False
            if global_cap is not None:
                projected = self.total_open_risk() + amount
                if projected > global_cap:
                    return False
            self._ledgers[mode_id] = ledger.record_reservation(amount)
            return True

    def release_reservation(self, mode_id: ModeId, amount: Money) -> None:
        ledger = self.get_ledger(mode_id)
        self._ledgers[mode_id] = ledger.release_reservation(amount)

    def record_fill(self, mode_id: ModeId, *, margin: Money, premium: Money) -> None:
        """Move a reservation into filled margin for an open position."""
        ledger = self.get_ledger(mode_id)
        self._ledgers[mode_id] = ledger.record_fill(margin, premium)

    def release_open_risk(self, mode_id: ModeId, amount: Money) -> None:
        """Release filled margin (or residual hold) when a position closes."""
        ledger = self.get_ledger(mode_id)
        zero = Money.zero(ledger.allocated_capital.currency)
        from_margin = min(ledger.margin_used, amount)
        remainder = amount - from_margin
        updated = ledger.model_copy(
            update={"margin_used": max(ledger.margin_used - from_margin, zero)}
        )
        if not remainder.is_zero and not remainder.is_negative:
            updated = updated.release_reservation(remainder)
        self._ledgers[mode_id] = updated

    def total_open_risk(self) -> Money:
        """Sum of open risk across all mode ledgers."""
        currency = self._total_equity.currency
        total = Money.zero(currency)
        for ledger in self._ledgers.values():
            total = total + ledger.open_risk
        return total

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
        "realized_gross_today": {},
        "prior_realized_gross": {},
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
    """Load pending entry holds only.

    ``COMMITTED`` capital is already represented as ``margin_used`` for open
    positions. Counting it here would double-count open risk after restart.
    """
    for res in store.list_reservations():
        if res.state is not ReservationState.RESERVED:
            continue
        m_id = res.mode_id or intent_to_mode.get(res.intent_id)
        if m_id is not None and m_id in reserved_by_mode:
            reserved_by_mode[m_id] = reserved_by_mode[m_id] + res.amount


def _index_order_events(store: TradingStore) -> dict[str, OrderEvent]:
    """Latest event per idempotency key; replayed duplicates stay idempotent."""
    indexed: dict[str, OrderEvent] = {}
    for stored in store.read_events():
        if stored.event_type is not TradingEventType.ORDER_EVENT:
            continue
        evt = stored.deserialize()
        if not isinstance(evt, OrderEvent):
            continue
        indexed[_order_dedupe_key(evt)] = evt
    return indexed


def _order_dedupe_key(event: OrderEvent) -> str:
    return event.identity.idempotency_key or event.identity.internal_order_id


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
            # Same quantum try_reserve / note_mode_fill used: max loss, not SPAN.
            open_risk = (
                record.risk_decision.recalculated_max_loss
                or record.risk_decision.margin_required
            )
            if open_risk is not None:
                balances["margin"][m_id] = balances["margin"][m_id] + open_risk
        elif pos_state is TradeState.CLOSED:
            trade_gross = _calculate_closed_trade_gross_pnl(
                record, orders_by_id, currency
            )
            close_date = record.as_of.date()
            if close_date == as_of_date:
                balances["realized_gross_today"][m_id] = (
                    balances["realized_gross_today"][m_id] + trade_gross
                )
            elif close_date < as_of_date:
                balances["prior_realized_gross"][m_id] = (
                    balances["prior_realized_gross"][m_id] + trade_gross
                )


def _assemble_final_ledgers(
    base_alloc: dict[ModeId, Money],
    balances: dict[str, dict[ModeId, Money]],
    currency: Currency,
) -> dict[ModeId, ModeLedger]:
    final_ledgers: dict[ModeId, ModeLedger] = {}
    for m, alloc_base in base_alloc.items():
        start_alloc = alloc_base + balances["prior_realized_gross"][m]
        realized_gross_today = balances["realized_gross_today"][m]
        realized_net_today = _net_realized_from_gross(realized_gross_today, currency)
        reserved = balances["reserved"][m]
        margin = balances["margin"][m]
        unrealized = balances["unrealized"][m]
        equity = start_alloc + realized_net_today + unrealized
        hwm = max(start_alloc, equity)
        drawdown = max(hwm - equity, Money.zero(currency))
        final_ledgers[m] = ModeLedger(
            mode_id=m,
            allocated_capital=start_alloc,
            realized_gross_pnl_today=realized_gross_today,
            realized_pnl_today=realized_net_today,
            unrealized_pnl=unrealized,
            reserved_capital=reserved,
            margin_used=margin,
            high_water_mark=hwm,
            drawdown=drawdown,
        )
    return final_ledgers


def _net_realized_from_gross(gross: Money, currency: Currency) -> Money:
    """Net realized P&L; equals gross until broker charges are on order events."""
    return gross.quantized(Rounding.HALF_EVEN)


def _calculate_closed_trade_gross_pnl(
    record: PositionLifecycleRecord,
    orders_by_id: dict[str, OrderEvent],
    currency: Currency,
) -> Money:
    """Signed fill-ledger gross cash flow for one closed trade (pre-charges)."""
    return _trade_fill_cash_flow(record.trade_id, orders_by_id, currency)


def _trade_fill_cash_flow(
    trade_id: str,
    orders_by_id: dict[str, OrderEvent],
    currency: Currency,
) -> Money:
    """Sum signed cash flows for each deduped fill belonging to one trade."""
    net_pnl_decimal = Decimal(0)
    saw_fill = False
    for order in orders_by_id.values():
        if order.identity.trade_id != trade_id:
            continue
        if order.state not in {OrderState.FILLED, OrderState.PARTIAL}:
            continue
        if order.average_fill_price is None or order.filled_quantity <= 0:
            continue
        saw_fill = True
        fill_val = order.average_fill_price.value * Decimal(order.filled_quantity)
        if order.command.side is Side.SELL:
            net_pnl_decimal += fill_val
        else:
            net_pnl_decimal -= fill_val
    if not saw_fill:
        return Money.zero(currency)
    return Money.of(str(net_pnl_decimal), currency).quantized(Rounding.HALF_EVEN)
