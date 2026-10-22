#!/usr/bin/env python3
"""Sweep M2 session flatten clock (carry-reject path) on Oracle chain replay."""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from trading.config.risk_policy import load_risk_policy
from trading.domain.clock import WallClock
from trading.domain.contracts.lifecycle import PositionLifecycleRecord
from trading.domain.enums import ModeId
from trading.domain.primitives import Money
from trading.portfolio.campaign_drawdown import trade_accounting
from trading.portfolio.fill_ledger import index_fill_charges, index_order_events
from trading.storage.trading_store import TradingStore

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from m2_positional_no_eod_replay import (
    IST,
    _is_carry_flatten_close,
    _session_date,
    load_chain_snaps,
)

FLATTEN_TIMES = (
    time(13, 30),
    time(14, 0),
    time(14, 30),
    time(15, 0),
    time(15, 10),
)


def _carry_approved_dates(rec: PositionLifecycleRecord) -> set[date]:
    out: set[date] = set()
    for row in rec.carry_records or ():
        if row.action.value != "CARRY_APPROVED":
            continue
        if row.as_of is not None:
            out.add(row.as_of.astimezone(IST).date())
    return out


def _exit_net(
    rec: PositionLifecycleRecord,
    entry_px: Decimal,
    qty: int,
    exit_bid: Decimal,
    charges_per_lot: Money,
) -> Decimal:
    gross = (exit_bid - entry_px) * Decimal(qty)
    lots = (
        rec.risk_decision.approved_legs[0].lots.count
        if rec.risk_decision.approved_legs
        else 1
    )
    charges = (charges_per_lot.amount * Decimal(lots) * 2).quantize(Decimal("0.01"))
    return gross - charges


def _bid_at_flatten(
    snaps: list[dict],
    sym: str,
    session_day: date,
    flatten_at: time,
    opened: datetime,
) -> tuple[Decimal | None, datetime | None]:
    """First chain bid on ``session_day`` at or after ``flatten_at`` (or entry)."""
    open_local = opened.astimezone(IST)
    cutoff = flatten_at
    if _session_date(opened) == session_day and open_local.timetz().replace(tzinfo=None) > cutoff:
        cutoff = open_local.timetz().replace(tzinfo=None)
    for snap in snaps:
        at = snap["at"]
        if at < opened:
            continue
        if _session_date(at) != session_day:
            continue
        local_t = at.astimezone(IST).timetz().replace(tzinfo=None)
        if local_t < cutoff:
            continue
        q = snap["quotes"].get(sym)
        if q is None:
            continue
        bid = q.get("bid")
        if bid is not None:
            return bid, at
    return None, None


def reprice_carry_flatten(
    rec: PositionLifecycleRecord,
    snaps: list[dict],
    charges_per_lot: Money,
    flatten_at: time,
    actual_net: Decimal,
) -> tuple[Decimal, datetime | None]:
    """Re-exit carry-gate flatten trades at ``flatten_at``; 15:00 uses book P&L."""
    if flatten_at == time(15, 0):
        return actual_net, rec.as_of
    leg = (rec.position.entry_legs or rec.position.legs)[0]
    sym = leg.contract.symbol
    opened = rec.position.opened_at
    if opened is None:
        return actual_net, rec.as_of
    close_day = rec.as_of.astimezone(IST).date()
    bid, at = _bid_at_flatten(snaps, sym, close_day, flatten_at, opened)
    if bid is None:
        return actual_net, rec.as_of
    qty = leg.quantity_contracts
    entry_px = leg.average_entry_price.value
    return _exit_net(rec, entry_px, qty, bid, charges_per_lot), at


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--from-date", default="2026-10-05")
    parser.add_argument("--to-date", default="2026-10-21")
    parser.add_argument("--data-to-date", default="2026-10-22")
    args = parser.parse_args()
    root = args.root.resolve()
    start = date.fromisoformat(args.from_date)
    end = date.fromisoformat(args.to_date)
    data_end = datetime.combine(
        date.fromisoformat(args.data_to_date), time(23, 59), tzinfo=IST
    )

    store = TradingStore.open(root / "data/paper/trading.sqlite", clock=WallClock())
    orders = index_order_events(store)
    charges_ix = index_fill_charges(store)
    cpl = load_risk_policy(root / "config/risk.yaml").config.charges_per_lot.to_money()

    records: list[PositionLifecycleRecord] = []
    symbols: set[str] = set()
    for _tid, state, payload in store._conn.execute(
        "select trade_id, state, payload from position_lifecycle"
    ):
        if state != "CLOSED":
            continue
        rec = PositionLifecycleRecord.model_validate_json(payload)
        mode = rec.mode_id or rec.position.mode_id
        if mode != ModeId.M2_DIRECTIONAL:
            continue
        opened = rec.position.opened_at
        if opened is None:
            continue
        open_d = opened.astimezone(IST).date()
        if open_d < start or open_d > end:
            continue
        records.append(rec)
        leg = (rec.position.entry_legs or rec.position.legs)[0]
        symbols.add(leg.contract.symbol)

    records.sort(key=lambda r: r.position.opened_at or datetime.min.replace(tzinfo=IST))
    snaps = load_chain_snaps(root, symbols, start, date.fromisoformat(args.data_to_date))

    actual_total = Decimal(0)
    for rec in records:
        actual_total += trade_accounting(
            rec, orders, charges_ix, charges_per_lot=cpl
        ).realized_net.amount

    rows: list[dict] = []
    for flatten_at in FLATTEN_TIMES:
        sim_total = Decimal(0)
        hold_hours: list[float] = []
        pinned = 0
        replayed = 0
        for rec in records:
            acct = trade_accounting(rec, orders, charges_ix, charges_per_lot=cpl)
            if _is_carry_flatten_close(rec):
                net, exit_at = reprice_carry_flatten(
                    rec,
                    snaps,
                    cpl,
                    flatten_at,
                    acct.realized_net.amount,
                )
                replayed += 1
            else:
                net = acct.realized_net.amount
                exit_at = rec.as_of
                pinned += 1
            sim_total += net
            opened = rec.position.opened_at
            if opened and exit_at:
                hold_hours.append((exit_at - opened).total_seconds() / 3600.0)

        rows.append(
            {
                "flatten_ist": flatten_at.strftime("%H:%M"),
                "net_pnl_inr": str(sim_total.quantize(Decimal("0.01"))),
                "trade_count": len(records),
                "avg_hold_hours": round(sum(hold_hours) / len(hold_hours), 2)
                if hold_hours
                else None,
                "pinned_trades": pinned,
                "replayed_trades": replayed,
                "delta_vs_actual_inr": str(
                    (sim_total - actual_total).quantize(Decimal("0.01"))
                ),
            }
        )

    peak = max(rows, key=lambda r: Decimal(r["net_pnl_inr"]))
    print(
        json.dumps(
            {
                "period_ist": f"{start}..{end}",
                "actual_net_inr": str(actual_total.quantize(Decimal("0.01"))),
                "m2_closed_trades": len(records),
                "by_flatten_time": rows,
                "peak_flatten_time": peak["flatten_ist"],
                "peak_net_pnl_inr": peak["net_pnl_inr"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
