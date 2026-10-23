#!/usr/bin/env python3
"""M2 edge checks on the Nifty paper book. Read-only.

Cutoffs below were fixed before reading results. Do not retune them on this
window. Fit is 5–16 Oct 2026. 19 Oct onward is held out and does not pick
a winner. There is no later month of paper history.

A check wins on the fit window only when its net is higher than the ledger
and its worst trade is not a larger loss than the ledger's worst trade.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from trading.config.risk_policy import load_risk_policy
from trading.domain.clock import WallClock
from trading.domain.contracts.lifecycle import PositionLifecycleRecord
from trading.domain.enums import ModeId
from trading.portfolio.campaign_drawdown import trade_accounting
from trading.portfolio.fill_ledger import index_fill_charges, index_order_events
from trading.storage.trading_store import TradingStore

IST = ZoneInfo("Asia/Kolkata")
WINDOW_START = date(2026, 10, 5)
WINDOW_END = date(2026, 10, 22)
FIT_END = date(2026, 10, 16)
HOLDOUT_START = date(2026, 10, 19)
BIG_MOVE_PCT = Decimal("0.45")
MOVE_START_PCT = Decimal("0.15")
# Same noise floor as strategies/_common.py MIN_TECHNICAL_MOVE_FRACTION.
R15_MIN = Decimal("0.001")
# Paper fallback already in config/discovery.yaml. Not refit here.
HOUR_SCORE = Decimal("0.30")
# Declared before the run. Not searched.
IV_RV_MAX = Decimal("1.3")
DELTA_LO = Decimal("0.45")
DELTA_HI = Decimal("0.65")
DELTA_TARGET = Decimal("0.55")
PREMIUM_STOP = Decimal("0.65")  # 35% below the entry price
PREMIUM_TARGET = Decimal("1.80")
FLATTEN_AT = time(15, 20)
QUOTE_SLACK = timedelta(minutes=20)
ANNUAL_BARS = Decimal(75 * 252)
ATR_BARS = 14
RV_MIN_RETURNS = 14


@dataclass(frozen=True)
class Quote:
    bid: Decimal | None
    ask: Decimal | None
    delta: Decimal | None
    iv: Decimal | None


@dataclass(frozen=True)
class BandOpt:
    symbol: str
    delta: Decimal
    side: str
    ask: Decimal
    bid: Decimal | None


@dataclass
class Snap:
    at: datetime
    spot: Decimal | None
    atm_iv: Decimal | None
    quotes: dict[str, Quote]
    band: tuple[BandOpt, ...]


@dataclass(frozen=True)
class Bar:
    start: datetime
    high: Decimal
    low: Decimal
    close: Decimal


@dataclass
class Point:
    at: datetime
    cycle_id: str
    r15: Decimal | None
    r60: Decimal | None
    score: Decimal | None
    reasons: frozenset[str]
    decision: str
    stage: str
    direction_blocked: bool


@dataclass
class BookTrade:
    trade_id: str
    direction: str
    symbol: str
    opened: datetime
    closed: datetime
    net: Decimal
    entry_px: Decimal
    qty: int
    lots: int
    hold_min: int
    source: str
    dte: int | None
    family_direction: str


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def session_date(ts: datetime) -> date:
    return ts.astimezone(IST).date()


def dec(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    return Decimal(str(value))


def r15_dir(r15: Decimal | None) -> str | None:
    if r15 is None or abs(r15) < R15_MIN:
        return None
    return "bull" if r15 > 0 else "bear"


def hour_dir(point: Point) -> str | None:
    if point.r15 is None or point.r60 is None or point.score is None:
        return None
    if point.r15 > 0 and point.r60 > 0 and point.score >= HOUR_SCORE:
        return "bull"
    if point.r15 < 0 and point.r60 < 0 and point.score <= -HOUR_SCORE:
        return "bear"
    return None


def money_net(entry: Decimal, exit_px: Decimal, qty: int, lots: int, charge: Decimal) -> Decimal:
    gross = (exit_px - entry) * Decimal(qty)
    charges = (charge * Decimal(lots) * 2).quantize(Decimal("0.01"))
    return (gross - charges).quantize(Decimal("0.01"))


def lower_median(values: list[int]) -> int:
    ordered = sorted(values)
    return ordered[(len(ordered) - 1) // 2]


def load_trades(
    store: TradingStore, orders: dict, charges_ix: dict, charge_money: object
) -> list[BookTrade]:
    rows: list[BookTrade] = []
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
        opened_day = session_date(opened)
        if opened_day < WINDOW_START or opened_day > WINDOW_END:
            continue
        acct = trade_accounting(
            rec, orders, charges_ix, charges_per_lot=charge_money
        )
        leg = (rec.position.entry_legs or rec.position.legs)[0]
        fam = str(rec.intent.family_id)
        if fam == "long_call":
            family_direction = "bull"
        elif fam == "long_put":
            family_direction = "bear"
        else:
            family_direction = "other"
        symbol = leg.contract.symbol
        if symbol.endswith("CE"):
            direction = "bull"
        elif symbol.endswith("PE"):
            direction = "bear"
        else:
            direction = family_direction
        expiry = leg.contract.expiry
        dte = (expiry - opened_day).days if expiry is not None else None
        lots = (
            rec.risk_decision.approved_legs[0].lots.count
            if rec.risk_decision.approved_legs
            else 1
        )
        rows.append(
            BookTrade(
                trade_id=rec.trade_id,
                direction=direction,
                symbol=symbol,
                opened=opened,
                closed=rec.as_of,
                net=acct.realized_net.amount,
                entry_px=leg.average_entry_price.value,
                qty=leg.quantity_contracts,
                lots=lots,
                hold_min=int((rec.as_of - opened).total_seconds() // 60),
                source="ledger",
                dte=dte,
                family_direction=family_direction,
            )
        )
    rows.sort(key=lambda row: row.opened)
    return rows


def load_points(store: TradingStore) -> tuple[list[Point], list[dict]]:
    slots: dict[str, dict[str, object]] = {}
    exits: list[dict] = []
    query = (
        "select payload from trading_events where event_type = 'discovery_decision'"
    )
    for (payload,) in store._conn.execute(query):
        blob = json.loads(payload)
        if blob.get("mode_id") != "M2_DIRECTIONAL":
            continue
        at = parse_ts(blob["as_of"])
        if session_date(at) < WINDOW_START or session_date(at) > WINDOW_END:
            continue
        stage = blob.get("stage") or ""
        if stage == "EXIT":
            exits.append(
                {
                    "at": at,
                    "rule": blob.get("exit_rule"),
                    "family": blob.get("family_id"),
                    "text": blob.get("reason_text"),
                }
            )
            continue
        reasons = frozenset(blob.get("reason_codes") or [])
        decision = blob.get("decision") or ""
        slot = slots.setdefault(
            blob["cycle_id"], {"point": None, "blocked": False, "traded": False}
        )
        if decision == "TRADE":
            slot["traded"] = True
        if direction_only_block(reasons, decision):
            slot["blocked"] = True
        if stage not in {"DATA", "DIRECTION", "BIND"}:
            continue
        inputs = blob.get("inputs") or {}
        r15 = dec(inputs.get("return_15m"))
        if r15 is None:
            continue
        current = slot["point"]
        if isinstance(current, Point) and current.at <= at:
            continue
        slot["point"] = Point(
            at=at,
            cycle_id=blob["cycle_id"],
            r15=r15,
            r60=dec(inputs.get("return_60m")),
            score=dec(inputs.get("trend_score")),
            reasons=reasons,
            decision=decision,
            stage=stage,
            direction_blocked=False,
        )
    points: list[Point] = []
    for slot in slots.values():
        point = slot["point"]
        if not isinstance(point, Point):
            continue
        points.append(
            Point(
                at=point.at,
                cycle_id=point.cycle_id,
                r15=point.r15,
                r60=point.r60,
                score=point.score,
                reasons=point.reasons,
                decision=point.decision,
                stage=point.stage,
                direction_blocked=bool(slot["blocked"]) and not bool(slot["traded"]),
            )
        )
    points.sort(key=lambda row: row.at)
    exits.sort(key=lambda row: row["at"])
    return points, exits


def direction_only_block(reasons: frozenset[str], decision: str) -> bool:
    if decision != "BLOCKED_HARD":
        return False
    if not reasons & {"DIRECTION_NEUTRAL", "DIRECTION_UNRESOLVED"}:
        return False
    other = reasons - {"DIRECTION_NEUTRAL", "DIRECTION_UNRESOLVED", "DATA_STALE"}
    return not other


def load_snaps(root: Path, symbols: set[str], day_from: date, day_to: date) -> list[Snap]:
    snaps: list[Snap] = []
    raw = root / "data/raw/fyers"
    for day_name in sorted(path.name for path in raw.iterdir() if path.is_dir()):
        try:
            day = date.fromisoformat(day_name)
        except ValueError:
            continue
        if day < day_from or day > day_to:
            continue
        kept = 0
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
            spot: Decimal | None = None
            parsed: list[dict] = []
            for row in chain:
                if row.get("strike_price") == -1 and "NIFTY50-INDEX" in (
                    row.get("symbol") or ""
                ):
                    if row.get("ltp") is not None:
                        spot = Decimal(str(row["ltp"]))
                    continue
                parsed.append(row)
            quotes: dict[str, Quote] = {}
            band: list[BandOpt] = []
            atm_strike: Decimal | None = None
            atm_gap: Decimal | None = None
            for row in parsed:
                strike = dec(row.get("strike_price"))
                if spot is not None and strike is not None and strike > 0:
                    gap = abs(strike - spot)
                    if atm_gap is None or gap < atm_gap:
                        atm_gap = gap
                        atm_strike = strike
            ce_iv: Decimal | None = None
            pe_iv: Decimal | None = None
            for row in parsed:
                sym = row.get("symbol") or ""
                greeks = row.get("greeks") or {}
                delta = dec(greeks.get("delta"))
                iv = dec(greeks.get("iv"))
                bid = dec(row.get("bid"))
                ask = dec(row.get("ask"))
                side = row.get("option_type") or ""
                if sym in symbols or ("NIFTY" in sym and side in {"CE", "PE"}):
                    quotes[sym] = Quote(bid=bid, ask=ask, delta=delta, iv=iv)
                abs_delta = abs(delta) if delta is not None else None
                if (
                    abs_delta is not None
                    and DELTA_LO <= abs_delta <= DELTA_HI
                    and ask is not None
                    and ask > 0
                    and side in {"CE", "PE"}
                ):
                    band.append(
                        BandOpt(
                            symbol=sym,
                            delta=abs_delta,
                            side=side,
                            ask=ask,
                            bid=bid,
                        )
                    )
                if atm_strike is not None and dec(row.get("strike_price")) == atm_strike:
                    if side == "CE":
                        ce_iv = iv
                    elif side == "PE":
                        pe_iv = iv
            ivs = [value for value in (ce_iv, pe_iv) if value is not None and value > 0]
            atm_iv = sum(ivs, Decimal(0)) / Decimal(len(ivs)) if ivs else None
            snaps.append(
                Snap(
                    at=parse_ts(blob["received_at"]),
                    spot=spot,
                    atm_iv=atm_iv,
                    quotes=quotes,
                    band=tuple(band),
                )
            )
            kept += 1
        print(f"chains {day.isoformat()} {kept}", file=sys.stderr)
    snaps.sort(key=lambda snap: snap.at)
    return snaps


def index_at(snaps: list[Snap], ts: datetime) -> int:
    lo = 0
    hi = len(snaps)
    while lo < hi:
        mid = (lo + hi) // 2
        if snaps[mid].at < ts:
            lo = mid + 1
        else:
            hi = mid
    return lo


def nearest(snaps: list[Snap], ts: datetime) -> Snap | None:
    if not snaps:
        return None
    idx = index_at(snaps, ts)
    choices: list[Snap] = []
    if idx < len(snaps):
        choices.append(snaps[idx])
    if idx > 0:
        choices.append(snaps[idx - 1])
    if not choices:
        return None
    best = min(choices, key=lambda snap: abs(snap.at - ts))
    if abs(best.at - ts) > QUOTE_SLACK:
        return None
    return best


def build_bars(snaps: list[Snap]) -> list[Bar]:
    buckets: dict[datetime, list[Decimal]] = {}
    for snap in snaps:
        if snap.spot is None:
            continue
        local = snap.at.astimezone(IST)
        if local.time() < time(9, 15) or local.time() > time(15, 30):
            continue
        minute = (local.minute // 5) * 5
        start = local.replace(minute=minute, second=0, microsecond=0)
        buckets.setdefault(start, []).append(snap.spot)
    bars: list[Bar] = []
    for start in sorted(buckets):
        values = buckets[start]
        bars.append(
            Bar(start=start, high=max(values), low=min(values), close=values[-1])
        )
    return bars


def completed_bars(bars: list[Bar], ts: datetime) -> list[Bar]:
    local = ts.astimezone(IST)
    return [bar for bar in bars if bar.start + timedelta(minutes=5) <= local]


def atr_at(bars: list[Bar], ts: datetime) -> Decimal | None:
    done = completed_bars(bars, ts)
    if len(done) < ATR_BARS:
        return None
    window = done[-ATR_BARS:]
    prev = done[-ATR_BARS - 1].close if len(done) > ATR_BARS else None
    total = Decimal(0)
    for bar in window:
        tr = bar.high - bar.low
        if prev is not None:
            tr = max(tr, abs(bar.high - prev), abs(bar.low - prev))
        total += tr
        prev = bar.close
    return total / Decimal(ATR_BARS)


def rv_at(bars: list[Bar], ts: datetime) -> Decimal | None:
    done = completed_bars(bars, ts)
    if len(done) < RV_MIN_RETURNS + 1:
        return None
    closes = [bar.close for bar in done[-51:]]
    returns = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]
    if len(returns) < RV_MIN_RETURNS:
        return None
    mean = sum(returns, Decimal(0)) / Decimal(len(returns))
    var = sum(((item - mean) ** 2 for item in returns), Decimal(0)) / Decimal(
        len(returns) - 1
    )
    if var <= 0:
        return None
    return var.sqrt() * ANNUAL_BARS.sqrt() * 100


def pick_band(snap: Snap, direction: str) -> BandOpt | None:
    side = "CE" if direction == "bull" else "PE"
    choices = [opt for opt in snap.band if opt.side == side]
    if not choices:
        return None
    choices.sort(key=lambda opt: abs(opt.delta - DELTA_TARGET))
    return choices[0]


def walk_premium(
    snaps: list[Snap],
    symbol: str,
    start: datetime,
    end: datetime,
    entry: Decimal,
) -> tuple[datetime, Decimal, str] | None:
    """Stop or target only after two quotes, matching discovery confirm_quotes."""
    stop_px = entry * PREMIUM_STOP
    target_px = entry * PREMIUM_TARGET
    last: tuple[datetime, Decimal] | None = None
    stop_hits = 0
    target_hits = 0
    idx = index_at(snaps, start)
    while idx < len(snaps) and snaps[idx].at <= end:
        quote = snaps[idx].quotes.get(symbol)
        if quote is not None and quote.bid is not None and quote.bid > 0:
            last = (snaps[idx].at, quote.bid)
            if quote.bid <= stop_px:
                stop_hits += 1
                target_hits = 0
                if stop_hits >= 2:
                    return snaps[idx].at, quote.bid, "premium_stop"
            elif quote.bid >= target_px:
                target_hits += 1
                stop_hits = 0
                if target_hits >= 2:
                    return snaps[idx].at, quote.bid, "target"
            else:
                stop_hits = 0
                target_hits = 0
        idx += 1
    if last is None:
        return None
    if end - last[0] > QUOTE_SLACK:
        return None
    return last[0], last[1], "held_to_flatten"


def walk_underlying(
    snaps: list[Snap],
    symbol: str,
    start: datetime,
    end: datetime,
    entry_spot: Decimal,
    atr: Decimal,
    direction: str,
) -> tuple[datetime, Decimal] | None:
    """Fire only when two quotes in a row show the index through the stop."""
    hits = 0
    idx = index_at(snaps, start)
    while idx < len(snaps) and snaps[idx].at <= end:
        snap = snaps[idx]
        spot = snap.spot
        breached = False
        if spot is not None:
            adverse = entry_spot - spot if direction == "bull" else spot - entry_spot
            breached = adverse >= atr
        if not breached:
            hits = 0
            idx += 1
            continue
        quote = snap.quotes.get(symbol)
        if quote is not None and quote.bid is not None and quote.bid > 0:
            hits += 1
            if hits >= 2:
                return snap.at, quote.bid
        idx += 1
    return None


def in_window(day: date, which: str) -> bool:
    if which == "fit":
        return WINDOW_START <= day <= FIT_END
    return HOLDOUT_START <= day <= WINDOW_END


def overlaps(trade: BookTrade, day: date) -> bool:
    return session_date(trade.opened) <= day <= session_date(trade.closed)


def stats(
    trades: list[BookTrade], which: str, big_days: list[dict]
) -> dict[str, object]:
    chosen = [row for row in trades if in_window(session_date(row.opened), which)]
    nets = [row.net for row in chosen]
    losers = [row for row in chosen if row.net < 0]
    total = sum(nets, Decimal(0))
    worst = min(nets) if nets else Decimal(0)
    hold = (
        round(sum(row.hold_min for row in losers) / len(losers))
        if losers
        else None
    )
    aligned = 0
    considered = 0
    for day in big_days:
        day_value = date.fromisoformat(day["date"])
        if not in_window(day_value, which):
            continue
        considered += 1
        if any(
            overlaps(row, day_value) and row.direction == day["move_dir"]
            for row in trades
        ):
            aligned += 1
    return {
        "trades": len(chosen),
        "net_inr": str(total.quantize(Decimal("0.01"))),
        "worst_loss_inr": str(worst.quantize(Decimal("0.01"))),
        "avg_loser_hold_min": hold,
        "big_days": considered,
        "big_days_with_aligned_position": aligned,
    }


def beats(check: dict[str, object], ledger: dict[str, object]) -> bool:
    if int(check["trades"]) == 0:
        return False
    net_ok = Decimal(str(check["net_inr"])) > Decimal(str(ledger["net_inr"]))
    loss_ok = Decimal(str(check["worst_loss_inr"])) >= Decimal(
        str(ledger["worst_loss_inr"])
    )
    return net_ok and loss_ok


def session_series(snaps: list[Snap]) -> dict[date, list[tuple[datetime, Decimal]]]:
    out: dict[date, list[tuple[datetime, Decimal]]] = {}
    for snap in snaps:
        if snap.spot is None:
            continue
        local = snap.at.astimezone(IST)
        if local.time() < time(9, 15) or local.time() > time(15, 30):
            continue
        out.setdefault(local.date(), []).append((snap.at, snap.spot))
    return out


def first_matching(points: list[Point], day: date, move_dir: str, kind: str) -> datetime | None:
    for point in points:
        if session_date(point.at) != day:
            continue
        found = hour_dir(point) if kind == "hour" else r15_dir(point.r15)
        if found == move_dir:
            return point.at
    return None


def exit_rule_for(exits: list[dict], trade: BookTrade) -> str:
    best: dict | None = None
    best_gap = timedelta(minutes=10)
    family = "long_call" if trade.direction == "bull" else "long_put"
    for row in exits:
        if row["family"] not in {family, None, ""}:
            continue
        gap = abs(row["at"] - trade.closed)
        if gap < best_gap:
            best = row
            best_gap = gap
    if best is None or not best.get("rule"):
        return "unrecorded"
    return str(best["rule"])


def main() -> None:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    store = TradingStore.open(root / "data/paper/trading.sqlite", clock=WallClock())
    orders = index_order_events(store)
    charges_ix = index_fill_charges(store)
    charge_money = load_risk_policy(
        root / "config/risk.yaml"
    ).config.charges_per_lot.to_money()
    charge = charge_money.amount
    ledger = load_trades(store, orders, charges_ix, charge_money)
    points, exits = load_points(store)
    symbols = {row.symbol for row in ledger}
    print(f"ledger {len(ledger)} points {len(points)}", file=sys.stderr)
    snaps = load_snaps(root, symbols, WINDOW_START, WINDOW_END)
    bars = build_bars(snaps)
    series = session_series(snaps)

    fit_qty = [row.qty for row in ledger if session_date(row.opened) <= FIT_END]
    fit_lots = [row.lots for row in ledger if session_date(row.opened) <= FIT_END]
    synth_qty = lower_median(fit_qty) if fit_qty else 1
    synth_lots = lower_median(fit_lots) if fit_lots else 1

    big_days: list[dict] = []
    for day, pts in sorted(series.items()):
        if not pts:
            continue
        open_px = pts[0][1]
        close_px = pts[-1][1]
        if open_px <= 0:
            continue
        move_pct = ((close_px - open_px) / open_px * 100).quantize(Decimal("0.01"))
        if abs(move_pct) < BIG_MOVE_PCT:
            continue
        move_dir = "bull" if close_px >= open_px else "bear"
        move_start: datetime | None = None
        for at, spot in pts:
            pct = (spot - open_px) / open_px * 100
            if move_dir == "bull" and pct >= MOVE_START_PCT:
                move_start = at
                break
            if move_dir == "bear" and pct <= -MOVE_START_PCT:
                move_start = at
                break
        hour_at = first_matching(points, day, move_dir, "hour")
        r15_at = first_matching(points, day, move_dir, "r15")

        def lag(signal: datetime | None) -> int | None:
            if signal is None or move_start is None:
                return None
            return int((signal - move_start).total_seconds() // 60)

        big_days.append(
            {
                "date": day.isoformat(),
                "session_pct": str(move_pct),
                "move_dir": move_dir,
                "move_start_ist": move_start.astimezone(IST).isoformat()
                if move_start
                else None,
                "hour_rule_signal_ist": hour_at.astimezone(IST).isoformat()
                if hour_at
                else None,
                "minutes_hour_rule_after_move_start": lag(hour_at),
                "r15_signal_ist": r15_at.astimezone(IST).isoformat() if r15_at else None,
                "minutes_r15_after_move_start": lag(r15_at),
            }
        )

    trade_rows: list[dict] = []
    for trade in ledger:
        snap = nearest(snaps, trade.opened)
        quote = snap.quotes.get(trade.symbol) if snap else None
        delta = quote.delta if quote else None
        dte = trade.dte
        atm = snap.atm_iv if snap else None
        realized = rv_at(bars, trade.opened)
        ratio = (atm / realized) if atm is not None and realized is not None and realized > 0 else None
        spot = snap.spot if snap else None
        atr = atr_at(bars, trade.opened)
        trade_rows.append(
            {
                "trade_id": trade.trade_id,
                "opened_ist": trade.opened.astimezone(IST).isoformat(),
                "closed_ist": trade.closed.astimezone(IST).isoformat(),
                "symbol": trade.symbol,
                "direction": trade.direction,
                "family_direction": trade.family_direction,
                "hold_min": trade.hold_min,
                "net_inr": str(trade.net),
                "delta": str(delta) if delta is not None else None,
                "dte": dte,
                "atm_iv_pct": str(atm.quantize(Decimal("0.01"))) if atm is not None else None,
                "realized_vol_pct": str(realized.quantize(Decimal("0.01")))
                if realized is not None
                else None,
                "iv_over_rv": str(ratio.quantize(Decimal("0.01"))) if ratio is not None else None,
                "rich_premium": bool(ratio is not None and ratio > IV_RV_MAX),
                "atr_points": str(atr.quantize(Decimal("0.01"))) if atr is not None else None,
                "spot": str(spot) if spot is not None else None,
                "exit_rule": exit_rule_for(exits, trade),
            }
        )

    # Check 1 — one extra entry when the 15-minute move is clear and the
    # hour window has not agreed. One open position. A new trade replaces any
    # book trade it overlaps. Two quotes must confirm a stop or target.
    earlier = list(ledger)
    occupied: list[tuple[datetime, datetime, str]] = [
        (row.opened, row.closed, row.trade_id) for row in ledger
    ]
    added = 0
    replaced = 0
    skipped_no_quote = 0
    prev_dir: str | None = None
    for point in points:
        current = r15_dir(point.r15)
        fresh = current is not None and current != prev_dir
        if current is not None:
            prev_dir = current
        if not fresh or current is None or hour_dir(point) is not None:
            continue
        if not point.direction_blocked:
            continue
        local_t = point.at.astimezone(IST).time()
        if local_t >= FLATTEN_AT:
            continue
        if any(lo <= point.at <= hi for lo, hi, _tid in occupied):
            continue
        snap = nearest(snaps, point.at)
        if snap is None:
            skipped_no_quote += 1
            continue
        chosen = pick_band(snap, current)
        if chosen is None:
            skipped_no_quote += 1
            continue
        flatten = datetime.combine(session_date(point.at), FLATTEN_AT, tzinfo=IST)
        walked = walk_premium(snaps, chosen.symbol, snap.at, flatten, chosen.ask)
        if walked is None:
            skipped_no_quote += 1
            continue
        exit_at, exit_bid, exit_reason = walked
        sim = BookTrade(
            trade_id=f"ADD-{point.cycle_id[:12]}",
            direction=current,
            symbol=chosen.symbol,
            opened=snap.at,
            closed=exit_at,
            net=money_net(chosen.ask, exit_bid, synth_qty, synth_lots, charge),
            entry_px=chosen.ask,
            qty=synth_qty,
            lots=synth_lots,
            hold_min=max(int((exit_at - snap.at).total_seconds() // 60), 0),
            source=f"added:{exit_reason}",
            dte=None,
            family_direction=current,
        )
        overlap_ids = {
            tid
            for lo, hi, tid in occupied
            if snap.at <= hi and lo <= exit_at
        }
        if overlap_ids:
            replaced += len(overlap_ids)
            earlier = [row for row in earlier if row.trade_id not in overlap_ids]
            occupied = [row for row in occupied if row[2] not in overlap_ids]
        earlier.append(sim)
        occupied.append((sim.opened, sim.closed, sim.trade_id))
        added += 1

    # Check 2 — drop ledger trades whose at-the-money option was rich.
    rich_book: list[BookTrade] = []
    skipped_rich = 0
    rich_unavailable = 0
    for trade, row in zip(ledger, trade_rows, strict=True):
        if row["iv_over_rv"] is None:
            rich_unavailable += 1
            rich_book.append(trade)
            continue
        if Decimal(str(row["iv_over_rv"])) > IV_RV_MAX:
            skipped_rich += 1
            continue
        rich_book.append(trade)

    # Check 3 — same entries. Leave the trade unless the index moves one
    # 14-bar range against it before the recorded exit.
    under_book: list[BookTrade] = []
    atr_fires = 0
    atr_unavailable = 0
    for trade, row in zip(ledger, trade_rows, strict=True):
        if row["atr_points"] is None or row["spot"] is None:
            atr_unavailable += 1
            under_book.append(trade)
            continue
        fired = walk_underlying(
            snaps,
            trade.symbol,
            trade.opened,
            trade.closed,
            Decimal(str(row["spot"])),
            Decimal(str(row["atr_points"])),
            trade.direction,
        )
        if fired is None:
            under_book.append(trade)
            continue
        exit_at, exit_bid = fired
        atr_fires += 1
        under_book.append(
            BookTrade(
                trade_id=trade.trade_id,
                direction=trade.direction,
                symbol=trade.symbol,
                opened=trade.opened,
                closed=exit_at,
                net=money_net(trade.entry_px, exit_bid, trade.qty, trade.lots, charge),
                entry_px=trade.entry_px,
                qty=trade.qty,
                lots=trade.lots,
                hold_min=int((exit_at - trade.opened).total_seconds() // 60),
                source="underlying_stop",
                dte=trade.dte,
                family_direction=trade.family_direction,
            )
        )

    checks = {
        "earlier_entry": earlier,
        "rich_premium_skip": rich_book,
        "underlying_stop": under_book,
    }
    ledger_fit = stats(ledger, "fit", big_days)
    ledger_hold = stats(ledger, "holdout", big_days)
    report_checks: dict[str, object] = {}
    winner: str | None = None
    winner_net = Decimal(str(ledger_fit["net_inr"]))
    for name, book in checks.items():
        fit = stats(book, "fit", big_days)
        hold = stats(book, "holdout", big_days)
        won = beats(fit, ledger_fit)
        report_checks[name] = {
            "fit": fit,
            "holdout": hold,
            "fit_minus_ledger_inr": str(
                (Decimal(str(fit["net_inr"])) - Decimal(str(ledger_fit["net_inr"]))).quantize(
                    Decimal("0.01")
                )
            ),
            "wins_on_fit": won,
        }
        if won and Decimal(str(fit["net_inr"])) > winner_net:
            winner = name
            winner_net = Decimal(str(fit["net_inr"]))

    changed = [
        {
            "trade_id": row.trade_id,
            "source": row.source,
            "opened_ist": row.opened.astimezone(IST).strftime("%Y-%m-%d %H:%M"),
            "closed_ist": row.closed.astimezone(IST).strftime("%Y-%m-%d %H:%M"),
            "direction": row.direction,
            "net_inr": str(row.net),
            "hold_min": row.hold_min,
        }
        for row in earlier
        if row.source != "ledger"
    ]
    stopped = [
        {
            "trade_id": row.trade_id,
            "opened_ist": row.opened.astimezone(IST).strftime("%Y-%m-%d %H:%M"),
            "closed_ist": row.closed.astimezone(IST).strftime("%Y-%m-%d %H:%M"),
            "net_inr": str(row.net),
            "hold_min": row.hold_min,
        }
        for row in under_book
        if row.source == "underlying_stop"
    ]
    report_checks["earlier_entry"]["changed_trades"] = changed
    report_checks["underlying_stop"]["stopped_trades"] = stopped
    report_checks["earlier_entry"]["replaced_book_trades"] = replaced
    report_checks["earlier_entry"]["added_trades"] = added
    report_checks["earlier_entry"]["skipped_no_quote"] = skipped_no_quote
    report_checks["earlier_entry"]["synthetic_qty"] = synth_qty
    report_checks["earlier_entry"]["synthetic_lots"] = synth_lots
    report_checks["rich_premium_skip"]["skipped_rich"] = skipped_rich
    report_checks["rich_premium_skip"]["iv_rv_unavailable"] = rich_unavailable
    report_checks["underlying_stop"]["atr_fires"] = atr_fires
    report_checks["underlying_stop"]["atr_unavailable"] = atr_unavailable

    print(
        json.dumps(
            {
                "window": {
                    "from": WINDOW_START.isoformat(),
                    "to": WINDOW_END.isoformat(),
                    "fit_through": FIT_END.isoformat(),
                    "holdout_from": HOLDOUT_START.isoformat(),
                },
                "predeclared": {
                    "r15_min": str(R15_MIN),
                    "hour_score": str(HOUR_SCORE),
                    "iv_over_rv_max": str(IV_RV_MAX),
                    "delta_band": [str(DELTA_LO), str(DELTA_HI)],
                    "underlying_stop": "1 x 14-bar five-minute average true range",
                    "win_rule": (
                        "Fit-window net higher than the ledger and worst loss "
                        "not larger. Holdout does not choose."
                    ),
                },
                "ledger": {"fit": ledger_fit, "holdout": ledger_hold},
                "chain_snaps": len(snaps),
                "five_minute_bars": len(bars),
                "big_move_days": big_days,
                "trades": trade_rows,
                "checks": report_checks,
                "winner": winner,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
