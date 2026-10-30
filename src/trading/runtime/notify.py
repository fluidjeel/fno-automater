"""Telegram copy for the paper session — plain language, no trading jargon."""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from trading.domain.contracts.lifecycle import PositionReviewRecord
from trading.domain.contracts.order import OrderEvent
from trading.domain.enums import OrderState, RiskAction, Side
from trading.domain.primitives import Currency
from trading.portfolio.fill_ledger import (
    index_order_events,
    is_chargeable_fill,
    trade_fill_cash_flow,
)
from trading.runtime.paper_runner import (
    LifecycleAlert,
    PaperCycleResult,
    PaperStrategyOutcome,
)
from trading.storage.trading_store import TradingStore

_IST = ZoneInfo("Asia/Kolkata")

__all__ = [
    "format_eod_report",
    "format_eod_report_for_session",
    "format_lifecycle_alert",
    "format_post_trade",
    "format_review_decision",
]


def format_post_trade(
    outcome: PaperStrategyOutcome,
    *,
    experiment_id: str,
    orders_by_id: dict[str, OrderEvent] | None = None,
) -> tuple[str, ...]:
    """One short line per entry or exit fill."""
    del experiment_id
    messages: list[str] = []
    for event in outcome.order_events:
        if event.state is not OrderState.FILLED:
            continue
        line = _fill_line(event, orders_by_id=orders_by_id)
        if line:
            messages.append(line)
    if not outcome.order_events:
        for decision in outcome.decisions:
            if decision.action is RiskAction.REJECT:
                messages.append("No new trade this cycle (blocked by rules).")
                break
    return tuple(messages)


def format_eod_report(
    results: Sequence[PaperCycleResult],
    *,
    open_trade_count: int,
    charges_verified: bool,
    session_date: date | None = None,
    store: TradingStore | None = None,
) -> str:
    """Simple end-of-day summary."""
    del results, charges_verified
    if store is not None and session_date is not None:
        return format_eod_report_for_session(store, session_date, open_trade_count)
    if open_trade_count:
        return (
            f"Day done: {open_trade_count} trade(s) still open; "
            "summary will follow when they close."
        )
    return "Day done: no trades today."


def format_eod_report_for_session(
    store: TradingStore,
    session_date: date,
    open_trade_count: int,
) -> str:
    """Totals from filled orders on the session date."""
    orders = index_order_events(store)
    by_trade: dict[str, list[OrderEvent]] = {}
    for event in orders.values():
        if not is_chargeable_fill(event):
            continue
        if event.received_at.astimezone(_IST).date() != session_date:
            continue
        by_trade.setdefault(event.identity.trade_id, []).append(event)
    closed = wins = losses = 0
    net = Decimal("0")
    for trade_id, fills in by_trade.items():
        if len(fills) < 2:
            continue
        closed += 1
        cash = trade_fill_cash_flow(trade_id, orders, Currency.INR).amount
        net += cash
        if cash > 0:
            wins += 1
        elif cash < 0:
            losses += 1
    pnl_words = _rupee_line(net)
    open_note = f" {open_trade_count} still open." if open_trade_count else ""
    return (
        f"Day done: {closed} trades, {wins} wins, {losses} losses, "
        f"net {pnl_words}.{open_note}"
    )


def format_lifecycle_alert(alert: LifecycleAlert) -> str:
    """Plain recovery notice."""
    return f"Check open trade {alert.trade_id}: {alert.detail}"


def format_review_decision(review: PositionReviewRecord) -> str:
    """Plain review line."""
    return f"Trade review: {review.action.value} on {review.trade_id}."


def _fill_line(
    event: OrderEvent,
    *,
    orders_by_id: dict[str, OrderEvent] | None,
) -> str:
    price = event.average_fill_price
    if price is None:
        return ""
    price_str = str(price.value.quantize(Decimal("0.01"))).removesuffix(".00")
    human = _human_symbol(event.command.contract.symbol)
    trade_id = event.identity.trade_id
    if orders_by_id is None:
        verb = "Bought" if event.command.side is Side.BUY else "Sold"
        return f"{verb} {human} at {price_str}"
    fills = sorted(
        (
            item
            for item in orders_by_id.values()
            if item.identity.trade_id == trade_id and is_chargeable_fill(item)
        ),
        key=lambda item: item.received_at,
    )
    if len(fills) >= 2 and fills[-1].event_id == event.event_id:
        entry = fills[0]
        exit_ev = fills[-1]
        entry_px = entry.average_fill_price
        exit_px = exit_ev.average_fill_price
        if entry_px is None or exit_px is None:
            return f"Closed {human} at {price_str}"
        entry_str = str(entry_px.value.quantize(Decimal("0.01")))
        exit_str = str(exit_px.value.quantize(Decimal("0.01")))
        cash = trade_fill_cash_flow(trade_id, orders_by_id, Currency.INR).amount
        entry_verb = "Bought" if entry.command.side is Side.BUY else "Sold"
        exit_verb = "Sold" if exit_ev.command.side is Side.SELL else "Bought"
        return (
            f"Closed {human}: {entry_verb.lower()} at {entry_str}, "
            f"{exit_verb.lower()} at {exit_str}, {_rupee_line(cash)}"
        )
    if len(fills) == 1 or fills[0].event_id == event.event_id:
        verb = "Bought" if event.command.side is Side.BUY else "Sold"
        return f"{verb} {human} at {price_str}"
    return ""


def _human_symbol(symbol: str) -> str:
    """Turn a contract code into 'Nifty 24500 call' when possible."""
    text = symbol.upper()
    strike_match = re.search(r"(\d{5})(CE|PE)\b", text)
    if strike_match:
        strike = strike_match.group(1)
        opt = "call" if strike_match.group(2) == "CE" else "put"
        return f"Nifty {strike} {opt}"
    if "NIFTY" in text:
        return "Nifty"
    return symbol.split(":")[-1].replace("-", " ").strip() or symbol


def _rupee_line(amount: Decimal) -> str:
    rounded = amount.quantize(Decimal("0.01"))
    if rounded > 0:
        return f"profit ₹{rounded}"
    if rounded < 0:
        return f"loss ₹{abs(rounded)}"
    return "flat ₹0"
