#!/usr/bin/env python3
"""Sweep discovery ``fallback_trend_threshold`` vs actual M2 book (Oracle window)."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from trading.config.discovery import load_discovery_config
from trading.config.risk_policy import load_risk_policy
from trading.domain.clock import WallClock
from trading.domain.contracts.lifecycle import PositionLifecycleRecord
from trading.domain.enums import (
    DiscoveryDecisionKind,
    DiscoveryStage,
    ModeId,
    ReasonCode,
    TradeState,
)
from trading.domain.primitives import Money, Price, TickSize
from trading.portfolio.campaign_drawdown import trade_accounting
from trading.portfolio.fill_ledger import index_fill_charges, index_order_events
from trading.runtime.discovery_decision_recorder import query_decisions
from trading.storage.trading_store import TradingStore
from trading.trade.exits import ExitEngine, ExitKind

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from m2_positional_no_eod_replay import (
    IST,
    _clear_time_exit,
    _session_date,
    _snap_for_leg,
    load_chain_snaps,
)

THRESHOLDS = (
    Decimal("0.30"),
    Decimal("0.25"),
    Decimal("0.20"),
    Decimal("0.15"),
)
BASELINE = Decimal("0.30")
FLATTEN_AT = time(15, 0)
TICK = TickSize.of("0.05")
MAJOR_MOVE_DAYS = (
    date(2026, 10, 6),
    date(2026, 10, 8),
    date(2026, 10, 9),
    date(2026, 10, 12),
    date(2026, 10, 13),
    date(2026, 10, 15),
    date(2026, 10, 19),
    date(2026, 10, 21),
)
DELTA_LO = Decimal("0.30")
DELTA_HI = Decimal("0.70")
DELTA_TARGET = Decimal("0.55")


def direction_bias(inp: object, threshold: Decimal) -> str | None:
    r15 = inp.return_15m
    r60 = inp.return_60m
    score = inp.trend_score
    if r15 is None or r60 is None or score is None:
        return None
    if r15 > 0 and r60 > 0 and score >= threshold:
        return "bull"
    if r15 < 0 and r60 < 0 and score <= -threshold:
        return "bear"
    return None


def direction_only_block(rows: list[object]) -> bool:
    for row in rows:
        if row.stage not in (DiscoveryStage.DATA, DiscoveryStage.BIND):
            continue
        codes = set(row.reason_codes)
        if not codes & {
            ReasonCode.DIRECTION_NEUTRAL,
            ReasonCode.DIRECTION_UNRESOLVED,
        }:
            continue
        if row.decision is not DiscoveryDecisionKind.BLOCKED_HARD:
            continue
        other = codes - {
            ReasonCode.DIRECTION_NEUTRAL,
            ReasonCode.DIRECTION_UNRESOLVED,
            ReasonCode.DATA_STALE,
        }
        if not other:
            return True
    return False


def load_all_decisions(
    store: TradingStore, start: date, end: date
) -> list[object]:
    rows: list[object] = []
    for i in range((end - start).days + 1):
        day = date.fromordinal(start.toordinal() + i)
        sess = datetime.combine(day, datetime.min.time(), tzinfo=IST)
        rows.extend(
            query_decisions(store, session_date=sess, mode_id=ModeId.M2_DIRECTIONAL)
        )
    return rows


def marginal_signals(
    by_cycle: dict[str, list[object]], threshold: Decimal
) -> list[tuple[datetime, str, str]]:
    if threshold >= BASELINE:
        return []
    out: list[tuple[datetime, str, str]] = []
    for cid, rows in by_cycle.items():
        if any(
            r.stage is DiscoveryStage.FILL
            and r.decision is DiscoveryDecisionKind.TRADE
            for r in rows
        ):
            continue
        data = [r for r in rows if r.stage is DiscoveryStage.DATA]
        if not data:
            continue
        inp = data[0].inputs
        if direction_bias(inp, threshold) is None:
            continue
        if direction_bias(inp, BASELINE) is not None:
            continue
        if not direction_only_block(rows):
            continue
        bias = direction_bias(inp, threshold)
        if bias is None:
            continue
        at = min(r.as_of for r in rows)
        out.append((at, bias, cid))
    out.sort(key=lambda x: x[0])
    return out


def pick_contract(
    root: Path, at: datetime, bias: str
) -> tuple[str, Decimal] | None:
    opt = "CE" if bias == "bull" else "PE"
    day_dir = root / "data/raw/fyers" / at.astimezone(IST).date().isoformat()
    if not day_dir.is_dir():
        return None
    best_path: Path | None = None
    best_skew = timedelta(hours=24)
    for path in day_dir.glob("*.json"):
        if path.stat().st_size < 8000:
            continue
        head = path.read_text(encoding="utf-8")[:240]
        if "options-chain-v3" not in head:
            continue
        blob = json.loads(path.read_text(encoding="utf-8"))
        if blob.get("endpoint") != "options-chain-v3":
            continue
        received = datetime.fromisoformat(blob["received_at"].replace("Z", "+00:00"))
        skew = abs(received - at)
        if skew < best_skew:
            best_skew = skew
            best_path = path
    if best_path is None or best_skew > timedelta(minutes=20):
        return None
    chain = (json.loads(best_path.read_text())["payload"]["data"]["optionsChain"])
    candidates: list[tuple[Decimal, str, Decimal]] = []
    for row in chain:
        sym = row.get("symbol") or ""
        if opt not in sym or "NIFTY" not in sym:
            continue
        if row.get("option_type") != opt:
            continue
        greeks = row.get("greeks") or {}
        delta = greeks.get("delta")
        if delta is None:
            continue
        delta = abs(Decimal(str(delta)))
        if delta < DELTA_LO or delta > DELTA_HI:
            continue
        ask = row.get("ask")
        if ask is None or Decimal(str(ask)) <= 0:
            continue
        candidates.append((abs(delta - DELTA_TARGET), sym, Decimal(str(ask))))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    _, sym, ask = candidates[0]
    return sym, ask


@dataclass
class TradeSim:
    trade_id: str
    opened_at: datetime
    closed_at: datetime
    net: Decimal
    entry_premium: Decimal
    direction: str
    synthetic: bool


def _exit_net(
    entry_px: Decimal,
    qty: int,
    exit_bid: Decimal,
    lots: int,
    charges_per_lot: Money,
) -> Decimal:
    gross = (exit_bid - entry_px) * Decimal(qty)
    charges = (charges_per_lot.amount * Decimal(lots) * 2).quantize(Decimal("0.01"))
    return gross - charges


def simulate_eod_l2(
    rec: PositionLifecycleRecord,
    snaps: list[dict],
    engine: ExitEngine,
    charges_per_lot: Money,
    data_end: datetime,
) -> TradeSim | None:
    leg = (rec.position.entry_legs or rec.position.legs)[0]
    sym = leg.contract.symbol
    opened = rec.position.opened_at
    if opened is None:
        return None
    qty = leg.quantity_contracts
    entry_px = leg.average_entry_price.value
    entry_premium = entry_px * Decimal(qty)
    lots = (
        rec.risk_decision.approved_legs[0].lots.count
        if rec.risk_decision.approved_legs
        else 1
    )
    policy = rec.position.exit_policy.model_copy(update={"time_exit": None})
    position = rec.position.model_copy(
        update={"state": TradeState.OPEN, "as_of": opened, "exit_policy": policy}
    )
    intent = rec.intent
    last_carry_day: date | None = None
    flattened_days: set[date] = set()

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
        local_t = at.astimezone(IST).timetz().replace(tzinfo=None)
        if last_carry_day != sess and local_t >= time(15, 0):
            policy = _clear_time_exit(policy, at)
            position = position.model_copy(update={"exit_policy": policy, "as_of": at})
            last_carry_day = sess
        if sess not in flattened_days and local_t >= FLATTEN_AT:
            bid = q.get("bid")
            if bid is not None:
                net = _exit_net(entry_px, qty, bid, lots, charges_per_lot)
                return TradeSim(
                    trade_id=rec.trade_id,
                    opened_at=opened,
                    closed_at=at,
                    net=net,
                    entry_premium=entry_premium,
                    direction="",
                    synthetic=True,
                )
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
                net = _exit_net(entry_px, qty, bid, lots, charges_per_lot)
                return TradeSim(
                    trade_id=rec.trade_id,
                    opened_at=opened,
                    closed_at=at,
                    net=net,
                    entry_premium=entry_premium,
                    direction="",
                    synthetic=True,
                )
    return None


def build_synthetic_rec(
    template: PositionLifecycleRecord,
    *,
    trade_id: str,
    symbol: str,
    entry_ask: Decimal,
    opened_at: datetime,
    bias: str,
) -> PositionLifecycleRecord | None:
    leg = (template.position.entry_legs or template.position.legs)[0]
    try:
        contract = leg.contract.model_copy(update={"symbol": symbol})
    except Exception:
        return None
    new_leg = leg.model_copy(
        update={
            "contract": contract,
            "average_entry_price": Price.snap(entry_ask, TICK),
        }
    )
    legs = (new_leg,)
    fam = "long_call" if bias == "bull" else "long_put"
    position = template.position.model_copy(
        update={
            "opened_at": opened_at,
            "as_of": opened_at,
            "entry_legs": legs,
            "legs": legs,
            "state": TradeState.OPEN,
        }
    )
    intent = template.intent.model_copy(update={"family_id": fam})
    return template.model_copy(
        update={
            "trade_id": trade_id,
            "as_of": opened_at,
            "position": position,
            "intent": intent,
        }
    )


def family_direction(fam: str, bias: str) -> str:
    if fam in ("long_call",):
        return "bull"
    if fam in ("long_put",):
        return "bear"
    return bias


def max_drawdown(trades: list[TradeSim]) -> Decimal:
    ordered = sorted(trades, key=lambda t: t.closed_at)
    cum = Decimal(0)
    peak = Decimal(0)
    max_dd = Decimal(0)
    for t in ordered:
        cum += t.net
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)
    return max_dd.quantize(Decimal("0.01"))


def peak_capital(trades: list[TradeSim]) -> Decimal:
    events: list[tuple[datetime, Decimal]] = []
    for t in trades:
        events.append((t.opened_at, t.entry_premium))
        events.append((t.closed_at, -t.entry_premium))
    events.sort(key=lambda x: x[0])
    open_cap = Decimal(0)
    peak = Decimal(0)
    for _, delta in events:
        open_cap += delta
        peak = max(peak, open_cap)
    return peak.quantize(Decimal("0.01"))


def caught_major_moves(trades: list[TradeSim], nifty_by_day: dict[date, Decimal]) -> int:
    caught = 0
    for day in MAJOR_MOVE_DAYS:
        move = nifty_by_day.get(day)
        if move is None or move == 0:
            continue
        move_dir = "bull" if move > 0 else "bear"
        sess_start = datetime.combine(day, time(9, 15), tzinfo=IST)
        sess_end = datetime.combine(day, time(15, 30), tzinfo=IST)
        aligned = any(
            t.direction == move_dir
            and t.opened_at <= sess_end
            and t.closed_at >= sess_start
            for t in trades
        )
        day_net = sum(
            t.net for t in trades if t.closed_at.astimezone(IST).date() == day
        )
        if aligned and day_net > 0:
            caught += 1
    return caught


def load_nifty_session_moves(root: Path, start: date, end: date) -> dict[date, Decimal]:
    from m2_big_move_catch_analysis import load_nifty_session_series, session_bounds

    series = load_nifty_session_series(root, start, end)
    out: dict[date, Decimal] = {}
    for day, pts in series.items():
        o, c, _, _ = session_bounds(pts)
        if o is not None and c is not None:
            out[day] = c - o
    return out


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

    actual_recs: list[PositionLifecycleRecord] = []
    template_call: PositionLifecycleRecord | None = None
    template_put: PositionLifecycleRecord | None = None
    for _tid, state, payload in store._conn.execute(
        "select trade_id, state, payload from position_lifecycle"
    ):
        if state != "CLOSED":
            continue
        rec = PositionLifecycleRecord.model_validate_json(payload)
        mode = rec.mode_id or rec.position.mode_id
        if mode is not ModeId.M2_DIRECTIONAL:
            continue
        opened = rec.position.opened_at
        if opened is None:
            continue
        od = opened.astimezone(IST).date()
        if od < start or od > end:
            continue
        actual_recs.append(rec)
        fam = str(rec.intent.family_id)
        if fam == "long_call" and template_call is None:
            template_call = rec
        if fam == "long_put" and template_put is None:
            template_put = rec
    actual_recs.sort(key=lambda r: r.position.opened_at or r.as_of)

    actual_trades: list[TradeSim] = []
    for rec in actual_recs:
        acct = trade_accounting(rec, orders, charges_ix, charges_per_lot=cpl)
        leg = (rec.position.entry_legs or rec.position.legs)[0]
        prem = leg.average_entry_price.value * Decimal(leg.quantity_contracts)
        fam = str(rec.intent.family_id)
        direction = "bull" if fam == "long_call" else "bear" if fam == "long_put" else ""
        actual_trades.append(
            TradeSim(
                trade_id=rec.trade_id,
                opened_at=rec.position.opened_at or rec.as_of,
                closed_at=rec.as_of,
                net=acct.realized_net.amount,
                entry_premium=prem,
                direction=direction,
                synthetic=False,
            )
        )

    decisions = load_all_decisions(store, start, end)
    by_cycle: dict[str, list[object]] = {}
    for row in decisions:
        by_cycle.setdefault(row.cycle_id, []).append(row)

    nifty_moves = load_nifty_session_moves(root, start, end)

    symbols: set[str] = set()
    for rec in actual_recs:
        leg = (rec.position.entry_legs or rec.position.legs)[0]
        symbols.add(leg.contract.symbol)

    # Pre-collect symbols for marginal signals (all thresholds).
    for threshold in THRESHOLDS:
        if threshold >= BASELINE:
            continue
        for at, bias, _cid in marginal_signals(by_cycle, threshold):
            picked = pick_contract(root, at, bias)
            if picked is not None:
                symbols.add(picked[0])

    snaps = load_chain_snaps(root, symbols, start, date.fromisoformat(args.data_to_date))

    sim_by_threshold: dict[str, list[TradeSim]] = {
        str(t): list(actual_trades) for t in THRESHOLDS
    }

    for threshold in THRESHOLDS:
        if threshold >= BASELINE:
            continue
        signals = marginal_signals(by_cycle, threshold)
        open_intervals = [(t.opened_at, t.closed_at) for t in actual_trades]
        for at, bias, cid in signals:
            if any(lo <= at <= hi for lo, hi in open_intervals):
                continue
            template = template_call if bias == "bull" else template_put
            if template is None:
                continue
            picked = pick_contract(root, at, bias)
            if picked is None:
                continue
            sym, ask = picked
            tid = f"SYN-{threshold}-{cid[:12]}"
            rec = build_synthetic_rec(
                template,
                trade_id=tid,
                symbol=sym,
                entry_ask=ask,
                opened_at=at,
                bias=bias,
            )
            if rec is None:
                continue
            sim = simulate_eod_l2(rec, snaps, engine, cpl, data_end)
            if sim is None:
                continue
            sim.direction = bias
            sim.synthetic = True
            sim.trade_id = tid
            sim_by_threshold[str(threshold)].append(sim)
            open_intervals.append((sim.opened_at, sim.closed_at))

    rows_out: list[dict[str, object]] = []
    for threshold in THRESHOLDS:
        trades = sim_by_threshold[str(threshold)]
        trades.sort(key=lambda t: t.closed_at)
        net = sum(t.net for t in trades)
        synths = [t for t in trades if t.synthetic]
        added_losing = sum(1 for t in synths if t.net < 0) if threshold != BASELINE else 0
        rows_out.append(
            {
                "fallback_trend_threshold": str(threshold),
                "trade_count": len(trades),
                "synthetic_trades_added": len(synths),
                "net_pnl_inr": str(net.quantize(Decimal("0.01"))),
                "major_moves_caught": caught_major_moves(trades, nifty_moves),
                "major_move_days": len(MAJOR_MOVE_DAYS),
                "added_losing_trades_vs_baseline": added_losing
                if threshold != BASELINE
                else 0,
                "max_drawdown_inr": str(max_drawdown(trades)),
                "peak_capital_inr": str(peak_capital(trades)),
            }
        )

    print(
        json.dumps(
            {
                "period_ist": f"{start}..{end}",
                "baseline_actual_trades": len(actual_trades),
                "method": (
                    "Actual book P&L pinned; marginal discovery cycles "
                    "(direction-only block at 0.30, pass at lower T) get "
                    "synthetic long_call/long_put from chain delta band "
                    "with L2 + 15:00 EOD flatten replay on sparse snaps."
                ),
                "caveats": [
                    "Marginal cycles never reached BIND in SQLite; option "
                    "pick is approximate vs live discovery bind.",
                    "Synthetic entries ignore concurrent-discovery hard blocks "
                    "(DATA_INVALID) on other cycles.",
                    "Overnight carry not modeled for synthetics.",
                ],
                "by_threshold": rows_out,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
