# ruff: noqa: PLR0912, PLR0915, PLR0917, F841, E501 - offline research script
#!/usr/bin/env python3
"""Sweep Layer 2 pnl_stop width for M2 positional replay (Oracle chain snapshots)."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, replace
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path

from trading.config.discovery import load_discovery_config
from trading.config.risk_policy import load_risk_policy
from trading.domain.clock import WallClock
from trading.domain.enums import ModeId, TradeState
from trading.domain.primitives import Money
from trading.portfolio.campaign_drawdown import trade_accounting
from trading.portfolio.fill_ledger import index_fill_charges, index_order_events
from trading.storage.trading_store import TradingStore
from trading.trade.exits import ExitEngine, ExitKind

sys.path.insert(0, str(Path(__file__).resolve().parent))

from m2_opening_range import OpeningRange, build_opening_ranges, opening_eval_instant
from m2_positional_no_eod_replay import (
    IST,
    SimTrade,
    _clear_time_exit,
    _held_overnight,
    _same_day_exit_before_carry,
    _session_date,
    _snap_for_leg,
    load_chain_snaps,
)

from trading.domain.contracts.lifecycle import PositionLifecycleRecord
from trading.trade.discovery_exits import structure_unrealized_pnl_mid

WIDTHS = (
    Decimal("1.00"),
    Decimal("1.25"),
    Decimal("1.50"),
    Decimal("1.75"),
    Decimal("2.00"),
)


def _scale_exit_policy(policy: object, width: Decimal) -> object:
    """Widen Layer 2 strategy pnl_stop by ``width`` (1.0 = frozen book policy)."""
    updates: dict[str, object] = {}
    if policy.pnl_stop is not None:
        updates["pnl_stop"] = Money(
            (policy.pnl_stop.amount * width).quantize(Decimal("0.01")),
            policy.pnl_stop.currency,
        )
    if policy.pnl_trail_stop is not None:
        updates["pnl_trail_stop"] = Money(
            (policy.pnl_trail_stop.amount * width).quantize(Decimal("0.01")),
            policy.pnl_trail_stop.currency,
        )
    if not updates:
        return policy
    return policy.model_copy(update=updates)


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


def _l2_stop_breached(
    policy: object,
    position: object,
    intent: object,
    leg_snaps: dict,
) -> bool:
    if policy.pnl_stop is None:
        return False
    pnl = structure_unrealized_pnl_mid(position, intent, leg_snaps)
    if pnl is None:
        return False
    return pnl.amount <= policy.pnl_stop.amount


def simulate_trade_width(
    rec: PositionLifecycleRecord,
    snaps: list[dict],
    engine: ExitEngine,
    charges_per_lot: Money,
    data_end: datetime,
    width: Decimal,
    opening_ranges: dict[tuple[date, str], OpeningRange],
) -> SimTrade:
    """Positional replay with widened L2 stop and 09:50 opening-range check."""
    leg = (rec.position.entry_legs or rec.position.legs)[0]
    sym = leg.contract.symbol
    opened = rec.position.opened_at
    if opened is None:
        raise ValueError("missing opened_at")
    qty = leg.quantity_contracts
    entry_px = leg.average_entry_price.value
    entry_premium = entry_px * Decimal(qty)
    policy = _scale_exit_policy(rec.position.exit_policy, width)
    policy = policy.model_copy(update={"time_exit": None})
    position = rec.position.model_copy(
        update={"state": TradeState.OPEN, "as_of": opened, "exit_policy": policy}
    )
    intent = rec.intent
    last_carry_day: date | None = None
    opening_checked: set[date] = set()
    held_overnight_sim = False
    stop_at_open = False

    for snap in snaps:
        at = snap["at"]
        if at < opened:
            continue
        if at > data_end:
            break
        q = snap["quotes"].get(sym)
        if q is None:
            continue
        sess = _session_date(at)
        if last_carry_day != sess and at.astimezone(IST).time() >= time(15, 0):
            policy = _clear_time_exit(policy, at)
            position = position.model_copy(update={"exit_policy": policy, "as_of": at})
            last_carry_day = sess
        if _session_date(opened) < sess:
            held_overnight_sim = True

        if _session_date(opened) < sess and sess not in opening_checked:
            opening_checked.add(sess)
            orng = opening_ranges.get((sess, sym))
            if orng is not None and orng.min_bid is not None:
                eval_at = opening_eval_instant(sess)
                worst = {
                    "bid": orng.min_bid,
                    "ask": orng.max_bid if orng.max_bid is not None else orng.min_bid,
                }
                feat = _snap_for_leg(leg, eval_at, worst)
                if feat is not None:
                    leg_snaps = {leg.leg_id: feat}
                    pos_eval = position.model_copy(update={"as_of": eval_at})
                    if _l2_stop_breached(policy, pos_eval, intent, leg_snaps):
                        sim_net = _exit_net(
                            rec, entry_px, qty, orng.min_bid, charges_per_lot
                        )
                        return SimTrade(
                            trade_id=rec.trade_id,
                            opened_at=opened,
                            closed_at=rec.as_of,
                            actual_net=Decimal(0),
                            sim_net=sim_net,
                            sim_exit_at=eval_at,
                            sim_exit_detail=(
                                "OPENING_RANGE_L2_STOP "
                                f"(range {orng.min_bid}-{orng.max_bid}, "
                                f"n={orng.observation_count})"
                            ),
                            held_overnight_original=_held_overnight(rec),
                            held_overnight_sim=True,
                            max_loss_hit_overnight=True,
                            entry_premium=entry_premium,
                            stop_at_open=True,
                        )

        if sess in opening_checked and at.astimezone(IST).time() < time(9, 50):
            continue

        feat = _snap_for_leg(leg, at, q)
        if feat is None:
            continue
        leg_snaps = {leg.leg_id: feat}
        position = position.model_copy(update={"as_of": at})
        ev = engine.evaluate(position, feat, intent, now=at, leg_snapshots=leg_snaps)
        if ev.updated_policy is not None:
            policy = ev.updated_policy
            position = position.model_copy(update={"exit_policy": policy})
        if ev.should_exit:
            bid = q.get("bid")
            if bid is not None:
                sim_net = _exit_net(rec, entry_px, qty, bid, charges_per_lot)
                overnight_l2 = (
                    held_overnight_sim
                    and ev.kind is ExitKind.STOP
                    and "P&L stop" in ev.detail
                )
                return SimTrade(
                    trade_id=rec.trade_id,
                    opened_at=opened,
                    closed_at=rec.as_of,
                    actual_net=Decimal(0),
                    sim_net=sim_net,
                    sim_exit_at=at,
                    sim_exit_detail=ev.detail,
                    held_overnight_original=_held_overnight(rec),
                    held_overnight_sim=held_overnight_sim,
                    max_loss_hit_overnight=overnight_l2,
                    entry_premium=entry_premium,
                    stop_at_open=False,
                )

    return SimTrade(
        trade_id=rec.trade_id,
        opened_at=opened,
        closed_at=rec.as_of,
        actual_net=Decimal(0),
        sim_net=None,
        sim_exit_at=None,
        sim_exit_detail="no_exit_in_data",
        held_overnight_original=_held_overnight(rec),
        held_overnight_sim=held_overnight_sim,
        max_loss_hit_overnight=False,
        entry_premium=entry_premium,
        stop_at_open=False,
    )


@dataclass(frozen=True, slots=True)
class SweepRow:
    label: str
    width_pct: str
    net_pnl_inr: Decimal
    trade_count: int
    overnight_stopped: int
    overnight_survived: int
    stops_at_open: int
    stops_later_overnight: int
    avg_loss_stopped: Decimal | None
    avg_win_survived: Decimal | None
    max_drawdown_inr: Decimal
    peak_capital_inr: Decimal


def _aggregate(
    rows: list[SimTrade],
) -> tuple[int, int, int, int, Decimal | None, Decimal | None]:
    stopped_pnls: list[Decimal] = []
    survived_wins: list[Decimal] = []
    n_stopped = 0
    n_survived = 0
    n_open = 0
    n_later = 0
    for r in rows:
        if not r.held_overnight_sim or r.sim_net is None:
            continue
        is_stop = (
            r.stop_at_open
            or r.max_loss_hit_overnight
            or (
                r.sim_exit_detail
                and (
                    "P&L stop" in r.sim_exit_detail
                    or "OPENING_RANGE" in r.sim_exit_detail
                )
            )
        )
        if is_stop:
            n_stopped += 1
            stopped_pnls.append(r.sim_net)
            if r.stop_at_open:
                n_open += 1
            else:
                n_later += 1
        else:
            n_survived += 1
            if r.sim_net > 0:
                survived_wins.append(r.sim_net)
    avg_loss = sum(stopped_pnls) / len(stopped_pnls) if stopped_pnls else None
    avg_win = sum(survived_wins) / len(survived_wins) if survived_wins else None
    return n_stopped, n_survived, n_open, n_later, avg_loss, avg_win


def _max_dd_and_peak(rows: list[SimTrade]) -> tuple[Decimal, Decimal]:
    peak_cap = max((r.entry_premium for r in rows), default=Decimal(0))
    cum = Decimal(0)
    peak = Decimal(0)
    trough = Decimal(0)
    for r in rows:
        net = r.sim_net if r.sim_net is not None else Decimal(0)
        cum += net
        peak = max(peak, cum)
        trough = min(trough, cum - r.entry_premium)
    return (peak - trough).quantize(Decimal("0.01")), peak_cap.quantize(Decimal("0.01"))


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
    disc = load_discovery_config(root / "config/discovery.yaml").config
    engine = ExitEngine(discovery_exits=disc.exits)

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
    snaps = load_chain_snaps(
        root, symbols, start, date.fromisoformat(args.data_to_date)
    )
    opening_ranges = build_opening_ranges(snaps, symbols)

    intraday_rows: list[SimTrade] = []
    actual_total = Decimal(0)
    for rec in records:
        acct = trade_accounting(rec, orders, charges_ix, charges_per_lot=cpl)
        actual_total += acct.realized_net.amount
        leg = (rec.position.entry_legs or rec.position.legs)[0]
        entry_premium = leg.average_entry_price.value * Decimal(leg.quantity_contracts)
        intraday_rows.append(
            SimTrade(
                trade_id=rec.trade_id,
                opened_at=rec.position.opened_at or rec.as_of,
                closed_at=rec.as_of,
                actual_net=acct.realized_net.amount,
                sim_net=acct.realized_net.amount,
                sim_exit_at=rec.as_of,
                sim_exit_detail="intraday_actual_book",
                held_overnight_original=_session_date(
                    rec.position.opened_at or rec.as_of
                )
                < _session_date(rec.as_of)
                if rec.position.opened_at
                else False,
                held_overnight_sim=False,
                max_loss_hit_overnight=False,
                entry_premium=entry_premium,
            )
        )

    def _intraday_overnight_stats() -> tuple[int, int, Decimal | None, Decimal | None]:
        losses: list[Decimal] = []
        wins: list[Decimal] = []
        for rec, row in zip(records, intraday_rows, strict=True):
            opened = rec.position.opened_at
            if opened is None:
                continue
            if _session_date(opened) >= _session_date(rec.as_of):
                continue
            net = row.sim_net or Decimal(0)
            if net < 0:
                losses.append(net)
            else:
                wins.append(net)
        n_l = len(losses)
        n_w = len(wins)
        return (
            n_l,
            n_w,
            sum(losses) / n_l if n_l else None,
            sum(wins) / n_w if n_w else None,
        )

    stopped_i, surv_i, al_i, aw_i = _intraday_overnight_stats()
    dd_i, peak_i = _max_dd_and_peak(intraday_rows)
    results: list[SweepRow] = [
        SweepRow(
            label="INTRADAY_EOD_ACTUAL",
            width_pct="n/a",
            net_pnl_inr=actual_total.quantize(Decimal("0.01")),
            trade_count=len(records),
            overnight_stopped=stopped_i,
            overnight_survived=surv_i,
            stops_at_open=0,
            stops_later_overnight=stopped_i,
            avg_loss_stopped=al_i.quantize(Decimal("0.01")) if al_i else None,
            avg_win_survived=aw_i.quantize(Decimal("0.01")) if aw_i else None,
            max_drawdown_inr=dd_i,
            peak_capital_inr=peak_i,
        )
    ]

    for width in WIDTHS:
        sim_rows: list[SimTrade] = []
        for rec in records:
            acct = trade_accounting(rec, orders, charges_ix, charges_per_lot=cpl)
            if _same_day_exit_before_carry(rec):
                leg = (rec.position.entry_legs or rec.position.legs)[0]
                entry_premium = leg.average_entry_price.value * Decimal(
                    leg.quantity_contracts
                )
                row = SimTrade(
                    trade_id=rec.trade_id,
                    opened_at=rec.position.opened_at or rec.as_of,
                    closed_at=rec.as_of,
                    actual_net=acct.realized_net.amount,
                    sim_net=acct.realized_net.amount,
                    sim_exit_at=rec.as_of,
                    sim_exit_detail="unchanged_same_day_pre_carry",
                    held_overnight_original=False,
                    held_overnight_sim=False,
                    max_loss_hit_overnight=False,
                    entry_premium=entry_premium,
                )
            else:
                row = simulate_trade_width(
                    rec, snaps, engine, cpl, data_end, width, opening_ranges
                )
                row = replace(row, actual_net=acct.realized_net.amount)
            sim_rows.append(row)

        total = sum((r.sim_net or Decimal(0) for r in sim_rows), Decimal(0))
        n_stop, n_surv, n_open, n_later, avg_l, avg_w = _aggregate(sim_rows)
        dd, peak = _max_dd_and_peak(sim_rows)
        pct = int(width * 100)
        results.append(
            SweepRow(
                label=f"POSITIONAL_L2_{pct}pct",
                width_pct=f"{pct}%",
                net_pnl_inr=total.quantize(Decimal("0.01")),
                trade_count=len(sim_rows),
                overnight_stopped=n_stop,
                overnight_survived=n_surv,
                stops_at_open=n_open,
                stops_later_overnight=n_later,
                avg_loss_stopped=avg_l.quantize(Decimal("0.01")) if avg_l else None,
                avg_win_survived=avg_w.quantize(Decimal("0.01")) if avg_w else None,
                max_drawdown_inr=dd,
                peak_capital_inr=peak,
            )
        )

    baseline = results[1].net_pnl_inr if len(results) > 1 else Decimal(0)
    table = []
    for row in results:
        delta = (
            row.net_pnl_inr - actual_total
            if row.label != "INTRADAY_EOD_ACTUAL"
            else Decimal(0)
        )
        vs_pos_100 = (
            row.net_pnl_inr - baseline
            if row.label.startswith("POSITIONAL") and row.width_pct != "100%"
            else None
        )
        table.append(
            {
                "scenario": row.label,
                "l2_width_pct": row.width_pct,
                "net_pnl_inr": str(row.net_pnl_inr),
                "vs_intraday_actual_inr": str(delta.quantize(Decimal("0.01")))
                if row.label != "INTRADAY_EOD_ACTUAL"
                else "0",
                "vs_positional_100pct_inr": str(vs_pos_100.quantize(Decimal("0.01")))
                if vs_pos_100 is not None
                else "n/a",
                "overnight_l2_stopped": row.overnight_stopped,
                "overnight_survived": row.overnight_survived,
                "stops_at_open": row.stops_at_open,
                "stops_later_in_session": row.stops_later_overnight,
                "avg_loss_stopped_inr": str(row.avg_loss_stopped)
                if row.avg_loss_stopped is not None
                else "n/a",
                "avg_win_survived_inr": str(row.avg_win_survived)
                if row.avg_win_survived is not None
                else "n/a",
                "max_drawdown_proxy_inr": str(row.max_drawdown_inr),
                "peak_capital_inr": str(row.peak_capital_inr),
            }
        )

    out = {
        "period_ist": f"{start}..{end}",
        "m2_trades": len(records),
        "opening_range_sessions_modelled": len({key[0] for key in opening_ranges}),
        "opening_range_symbol_days": len(opening_ranges),
        "caveats": [
            "Positional paths use sparse Fyers options-chain-v3 snapshots, not tick quotes.",
            "Opening range: min/max bid in 09:15-09:35 IST; L2 checked at 09:50 on worst bid.",
            "Same-day exits before 15:00 carry gate are pinned to actual book P&L.",
            "INTRADAY_EOD_ACTUAL is realized book (no open-range re-model on that row).",
            "Wider L2 scales frozen pnl_stop and pnl_trail_stop on the exit policy.",
        ],
        "comparison_table": table,
    }
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
