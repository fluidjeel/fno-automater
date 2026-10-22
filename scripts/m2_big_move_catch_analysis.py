#!/usr/bin/env python3
"""M2 caught vs missed major Nifty session moves (Oracle paper window)."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from trading.config.risk_policy import load_risk_policy
from trading.domain.clock import WallClock
from trading.domain.contracts.lifecycle import PositionLifecycleRecord
from trading.domain.enums import ModeId, Side
from trading.portfolio.campaign_drawdown import trade_accounting
from trading.portfolio.fill_ledger import (
    index_fill_charges,
    index_order_events,
    is_chargeable_fill,
)
from trading.storage.trading_store import TradingStore

IST = ZoneInfo("Asia/Kolkata")
SESSION_OPEN = time(9, 15)
SESSION_CLOSE = time(15, 30)
EOD_FLATTEN_CUTOFF = time(15, 0)
NIFTY_LOT = 75
BIG_MOVE_PCT = Decimal("0.45")


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def load_nifty_session_series(
    root: Path, day_from: date, day_to: date
) -> dict[date, list[tuple[datetime, Decimal]]]:
    """Index LTP samples per calendar day from options-chain-v3."""
    by_day: dict[date, list[tuple[datetime, Decimal]]] = defaultdict(list)
    raw = root / "data/raw/fyers"
    if not raw.is_dir():
        return by_day
    for day_name in sorted(p.name for p in raw.iterdir() if p.is_dir()):
        try:
            day = date.fromisoformat(day_name)
        except ValueError:
            continue
        if day < day_from or day > day_to:
            continue
        for path in (raw / day_name).glob("*.json"):
            if path.stat().st_size < 8000:
                continue
            head = path.read_text(encoding="utf-8")[:240]
            if "options-chain-v3" not in head:
                continue
            blob = json.loads(path.read_text(encoding="utf-8"))
            if blob.get("endpoint") != "options-chain-v3":
                continue
            chain = (blob.get("payload") or {}).get("data", {}).get("optionsChain") or []
            ltp: Decimal | None = None
            for row in chain:
                if row.get("strike_price") == -1 and "NIFTY50-INDEX" in (
                    row.get("symbol") or ""
                ):
                    ltp = Decimal(str(row["ltp"]))
                    break
            if ltp is None:
                continue
            ts = parse_ts(blob["received_at"]).astimezone(IST)
            by_day[day].append((ts, ltp))
    for pts in by_day.values():
        pts.sort(key=lambda x: x[0])
    return by_day


def session_bounds(
    pts: list[tuple[datetime, Decimal]],
) -> tuple[Decimal | None, Decimal | None, Decimal | None, Decimal | None]:
    """Return open, close, ~15:00, and last pre-15:30 samples."""
    if not pts:
        return None, None, None, None
    open_px = None
    close_px = None
    at_1500 = None
    last_pre_1500 = None
    for ts, px in pts:
        t = ts.timetz().replace(tzinfo=None)
        if t >= SESSION_OPEN and open_px is None:
            open_px = px
        if t <= SESSION_CLOSE:
            close_px = px
        if t <= EOD_FLATTEN_CUTOFF:
            last_pre_1500 = px
        if EOD_FLATTEN_CUTOFF <= t <= time(15, 5):
            at_1500 = px
    if at_1500 is None:
        at_1500 = last_pre_1500
    return open_px, close_px, at_1500, close_px


def family_direction(family: str) -> str:
    fam = (family or "").lower()
    if fam in {
        "long_call",
        "bull_call_debit",
        "bull_put_credit",
        "long_call_butterfly",
        "long_call_calendar",
    }:
        return "bull"
    if fam in {
        "long_put",
        "bear_put_debit",
        "bear_call_credit",
        "long_put_butterfly",
        "long_put_calendar",
    }:
        return "bear"
    if fam in {"long_straddle", "long_strangle"}:
        return "vol"
    return "neutral"


def trade_direction(rec: PositionLifecycleRecord) -> str:
    fam = family_direction(
        rec.intent.family_id.value
        if hasattr(rec.intent.family_id, "value")
        else str(rec.intent.family_id)
    )
    if fam != "neutral":
        return fam
    legs = rec.position.entry_legs or rec.position.legs
    if len(legs) == 1:
        leg = legs[0]
        opt = (leg.contract.option_type or "").upper()
        side = leg.side
        if opt == "CE" and side is Side.BUY:
            return "bull"
        if opt == "PE" and side is Side.BUY:
            return "bear"
        if opt == "CE" and side is Side.SELL:
            return "bear"
        if opt == "PE" and side is Side.SELL:
            return "bull"
    return "neutral"


def load_m2_records(
    store: TradingStore, start: date, end: date
) -> list[PositionLifecycleRecord]:
    out: list[PositionLifecycleRecord] = []
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
        closed_d = rec.as_of.astimezone(IST).date()
        if closed_d < start or open_d > end:
            continue
        out.append(rec)
    out.sort(key=lambda r: r.position.opened_at or r.as_of)
    return out


def fills_on_day(
    trade_id: str,
    orders: dict,
    charges_ix: dict,
    day: date,
) -> tuple[Decimal, Decimal]:
    """Gross and confirmed charges for fills on calendar day (IST)."""
    gross = Decimal(0)
    charges = Decimal(0)
    for order in orders.values():
        if order.identity.trade_id != trade_id or not is_chargeable_fill(order):
            continue
        ts = order.received_at.astimezone(IST).date()
        if ts != day:
            continue
        px = order.average_fill_price
        if px is None:
            continue
        val = px.value * Decimal(order.filled_quantity)
        if order.command.side is Side.SELL:
            gross += val
        else:
            gross -= val
        key = order.identity.idempotency_key or order.identity.internal_order_id
        ch = charges_ix.get(key)
        if ch is not None:
            charges += ch.total_charges.amount
    return gross, charges


def active_trades_on_day(
    records: list[PositionLifecycleRecord], day: date
) -> list[tuple[PositionLifecycleRecord, str]]:
    start = datetime.combine(day, SESSION_OPEN, tzinfo=IST)
    end = datetime.combine(day, SESSION_CLOSE, tzinfo=IST)
    active: list[tuple[PositionLifecycleRecord, str]] = []
    for rec in records:
        opened = rec.position.opened_at
        if opened is None:
            continue
        closed = rec.as_of
        if opened > end or closed < start:
            continue
        active.append((rec, trade_direction(rec)))
    return active


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--from-date", default="2026-10-05")
    parser.add_argument("--to-date", default="2026-10-21")
    parser.add_argument("--top-n", type=int, default=0, help="0 = threshold only")
    args = parser.parse_args()
    root = args.root.resolve()
    start = date.fromisoformat(args.from_date)
    end = date.fromisoformat(args.to_date)

    series = load_nifty_session_series(root, start, end)
    store = TradingStore.open(root / "data/paper/trading.sqlite", clock=WallClock())
    orders = index_order_events(store)
    charges_ix = index_fill_charges(store)
    cpl = load_risk_policy(root / "config/risk.yaml").config.charges_per_lot.to_money()
    records = load_m2_records(store, start, end)

    daily: list[dict] = []
    sorted_days = sorted(d for d in series if start <= d <= end)
    prev_close: Decimal | None = None
    for day in sorted_days:
        open_px, close_px, px_1500, _ = session_bounds(series[day])
        if open_px is None or close_px is None:
            continue
        pts_chg = close_px - open_px
        pts_pct = (pts_chg / open_px * 100).quantize(Decimal("0.01"))
        c2c = None
        c2c_pct = None
        if prev_close is not None:
            c2c = close_px - prev_close
            c2c_pct = (c2c / prev_close * 100).quantize(Decimal("0.01"))
        post_3pm = None
        if px_1500 is not None:
            post_3pm = (close_px - px_1500).quantize(Decimal("0.01"))
        prev_close = close_px
        daily.append(
            {
                "date": day.isoformat(),
                "open": str(open_px),
                "close": str(close_px),
                "at_1500": str(px_1500) if px_1500 else None,
                "session_pts": str(pts_chg.quantize(Decimal("0.01"))),
                "session_pct": str(pts_pct),
                "c2c_pts": str(c2c.quantize(Decimal("0.01"))) if c2c else None,
                "c2c_pct": str(c2c_pct) if c2c_pct else None,
                "post_3pm_pts": str(post_3pm) if post_3pm is not None else None,
            }
        )

    # Major moves: |session_pct| >= threshold, plus ensure we include top movers
    majors = [d for d in daily if abs(Decimal(d["session_pct"])) >= BIG_MOVE_PCT]
    if args.top_n > 0:
        by_abs = sorted(daily, key=lambda x: abs(Decimal(x["session_pct"])), reverse=True)
        for row in by_abs[: args.top_n]:
            if row not in majors:
                majors.append(row)
    majors.sort(key=lambda x: x["date"])

    move_rows: list[dict] = []
    caught = 0
    missed = 0
    pnl_caught = Decimal(0)
    pnl_missed = Decimal(0)

    for row in majors:
        day = date.fromisoformat(row["date"])
        move_dir = "up" if Decimal(row["session_pts"]) > 0 else "down"
        active = active_trades_on_day(records, day)
        dirs = {d for _, d in active}
        exposure = "flat"
        if "bull" in dirs and "bear" not in dirs:
            exposure = "bull"
        elif "bear" in dirs and "bull" not in dirs:
            exposure = "bear"
        elif dirs - {"neutral", "vol"}:
            exposure = "mixed"
        elif "vol" in dirs:
            exposure = "vol"

        aligned = (move_dir == "up" and exposure == "bull") or (
            move_dir == "down" and exposure == "bear"
        )
        opposite = (move_dir == "up" and exposure == "bear") or (
            move_dir == "down" and exposure == "bull"
        )

        # P&L on move day and next session day
        m2_day_net = Decimal(0)
        m2_next_net = Decimal(0)
        next_day = day + timedelta(days=1)
        while next_day.weekday() >= 5:
            next_day += timedelta(days=1)
        for rec in records:
            g0, c0 = fills_on_day(rec.trade_id, orders, charges_ix, day)
            m2_day_net += g0 - c0
            g1, c1 = fills_on_day(rec.trade_id, orders, charges_ix, next_day)
            m2_next_net += g1 - c1

        # Also attribute full trade net if closed on move day or next
        close_attr = Decimal(0)
        for rec in records:
            acct = trade_accounting(rec, orders, charges_ix, charges_per_lot=cpl)
            cd = rec.as_of.astimezone(IST).date()
            if cd == day or cd == next_day:
                close_attr += acct.realized_net.amount

        bench_pts = Decimal(row["session_pts"])
        bench_pnl = (bench_pts * NIFTY_LOT).quantize(Decimal("0.01"))
        if move_dir == "down":
            bench_pnl = -bench_pnl

        # Caught: aligned exposure and positive attributed P&L on day or next
        attr = m2_day_net if m2_day_net != 0 else close_attr
        if m2_day_net == 0 and close_attr != 0:
            attr = close_attr
        elif m2_day_net != 0 and m2_next_net != 0:
            attr = m2_day_net + m2_next_net
        elif m2_day_net == 0 and m2_next_net != 0:
            attr = m2_next_net

        if exposure == "flat":
            label = "missed_flat"
            missed += 1
            pnl_missed += attr
        elif opposite or exposure == "mixed":
            label = "missed_opposite_or_mixed"
            missed += 1
            pnl_missed += attr
        elif aligned:
            if attr > 0:
                label = "caught_profit"
                caught += 1
                pnl_caught += attr
            else:
                label = "caught_exposure_loss"
                missed += 1
                pnl_missed += attr
        else:
            label = "neutral_vol"
            missed += 1
            pnl_missed += attr

        move_rows.append(
            {
                "date": row["date"],
                "nifty_open": row["open"],
                "nifty_close": row["close"],
                "session_move_pts": row["session_pts"],
                "session_move_pct": row["session_pct"],
                "post_3pm_pts": row["post_3pm_pts"],
                "move_dir": move_dir,
                "m2_exposure": exposure,
                "active_trades": [
                    {
                        "trade_id": rec.trade_id,
                        "family": str(rec.intent.family_id),
                        "direction": d,
                        "opened": rec.position.opened_at.isoformat()
                        if rec.position.opened_at
                        else None,
                        "closed": rec.as_of.isoformat(),
                    }
                    for rec, d in active
                ],
                "m2_fill_pnl_day": str(m2_day_net.quantize(Decimal("0.01"))),
                "m2_fill_pnl_next": str(m2_next_net.quantize(Decimal("0.01"))),
                "m2_close_attrib_day_or_next": str(close_attr.quantize(Decimal("0.01"))),
                "m2_pnl_used": str(attr.quantize(Decimal("0.01"))),
                "bench_1lot_nifty_pnl": str(bench_pnl),
                "label": label,
            }
        )

    print(
        json.dumps(
            {
                "window": {"from": start.isoformat(), "to": end.isoformat()},
                "big_move_threshold_session_pct": str(BIG_MOVE_PCT),
                "nifty_lot_assumption": NIFTY_LOT,
                "all_session_days": daily,
                "major_move_days": move_rows,
                "summary": {
                    "major_move_count": len(move_rows),
                    "caught_profit_days": caught,
                    "missed_or_wrong_days": missed,
                    "m2_net_pnl_on_caught_days": str(pnl_caught.quantize(Decimal("0.01"))),
                    "m2_net_pnl_on_missed_days": str(pnl_missed.quantize(Decimal("0.01"))),
                    "total_m2_closed_in_window": len(records),
                },
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
