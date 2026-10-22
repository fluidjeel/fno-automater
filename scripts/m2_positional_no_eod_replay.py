"""Counterfactual M2 replay: same entries, discovery exits, no M2 carry/EOD flatten."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from trading.config.discovery import load_discovery_config
from trading.config.risk_policy import load_risk_policy
from trading.domain.clock import WallClock
from trading.domain.contracts import (
    DataQualityReport,
    FeatureSnapshot,
    Lineage,
    MarketQuote,
    SnapshotTimes,
    Versions,
)
from trading.domain.contracts.lifecycle import PositionLifecycleRecord
from trading.domain.contracts.snapshot import DerivativesContext
from trading.domain.enums import DataQuality, ModeId, TradeState
from trading.domain.primitives import Currency, Money, Price, TickSize
from trading.portfolio.campaign_drawdown import trade_accounting
from trading.portfolio.fill_ledger import index_fill_charges, index_order_events
from trading.storage.trading_store import TradingStore
from trading.trade.exits import ExitEngine, ExitKind

IST = ZoneInfo("Asia/Kolkata")
TICK = TickSize.of("0.05")


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def load_chain_snaps(
    root: Path, symbols: set[str], day_from: date, day_to: date
) -> list[dict]:
    snaps: list[dict] = []
    raw = root / "data/raw/fyers"
    if not raw.is_dir():
        return snaps
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
            ts = parse_ts(blob["received_at"])
            quotes: dict[str, dict[str, Decimal | None]] = {}
            for row in chain:
                sym = row.get("symbol") or ""
                if sym not in symbols:
                    continue
                quotes[sym] = {
                    "bid": Decimal(str(row["bid"])) if row.get("bid") is not None else None,
                    "ask": Decimal(str(row["ask"])) if row.get("ask") is not None else None,
                }
            if quotes:
                snaps.append({"at": ts, "quotes": quotes})
    snaps.sort(key=lambda item: item["at"])
    return snaps


def _snap_for_leg(
    leg: object,
    at: datetime,
    quote: dict[str, Decimal | None],
) -> FeatureSnapshot | None:
    bid, ask = quote.get("bid"), quote.get("ask")
    if bid is None and ask is None:
        return None
    if bid is None:
        bid = ask
    if ask is None:
        ask = bid
    contract = leg.contract
    expiry = contract.expiry
    dte = (expiry - at.astimezone(IST).date()).days if expiry else 5
    last = (bid + ask) / 2
    return FeatureSnapshot(
        snapshot_id=f"REPLAY-{contract.symbol}-{int(at.timestamp())}",
        contract=contract,
        market=MarketQuote(
            bid=Price.snap(bid, TICK),
            ask=Price.snap(ask, TICK),
            last=Price.snap(last, TICK),
        ),
        times=SnapshotTimes(
            event_time=at - timedelta(seconds=1),
            source_time=at - timedelta(seconds=1),
            receive_time=at,
            calculation_time=at,
        ),
        feature_set_version="1",
        features={"realized_vol_20d": Decimal("0.14")},
        quality=DataQualityReport(
            state=DataQuality.VALID,
            age_ms=120,
            warmup_complete=True,
            source_status="connected",
            reason_codes=(),
        ),
        lineage=Lineage(
            provider="replay",
            source_ids=("chain",),
            raw_event_refs=("replay",),
            normalization_version="1",
            versions=Versions(
                code_version="0.1.0", config_version="1", config_checksum="replay"
            ),
        ),
        derivatives=DerivativesContext(
            days_to_expiry=max(dte, 0),
            open_interest=5000,
            option_type=contract.option_type,
            underlying_price=Price(Decimal("24000"), TICK),
        ),
    )


def _clear_time_exit(policy: object, now: datetime) -> object:
    return policy.model_copy(update={"time_exit": None})


def _session_date(dt: datetime) -> date:
    return dt.astimezone(IST).date()


def _is_carry_flatten_close(rec: PositionLifecycleRecord) -> bool:
    closed = rec.as_of.astimezone(IST)
    if closed.hour == 15 and closed.minute <= 5:
        carries = rec.carry_records or ()
        if any(c.action.value == "CARRY_REJECTED" for c in carries):
            return True
    return False


def _same_day_exit_before_carry(rec: PositionLifecycleRecord) -> bool:
    """Trades that left before the M2 carry gate; economics unchanged without EOD."""
    opened = rec.position.opened_at
    if opened is None:
        return False
    if _session_date(opened) != _session_date(rec.as_of):
        return False
    closed = rec.as_of.astimezone(IST)
    if closed.hour < 15:
        return True
    return not _is_carry_flatten_close(rec)


@dataclass
class SimTrade:
    trade_id: str
    opened_at: datetime
    closed_at: datetime | None
    actual_net: Decimal
    sim_net: Decimal | None
    sim_exit_at: datetime | None
    sim_exit_detail: str | None
    held_overnight_original: bool
    held_overnight_sim: bool
    max_loss_hit_overnight: bool
    entry_premium: Decimal
    stop_at_open: bool = False


def simulate_trade(
    rec: PositionLifecycleRecord,
    snaps: list[dict],
    engine: ExitEngine,
    charges_per_lot: Money,
    data_end: datetime,
) -> SimTrade:
    leg = (rec.position.entry_legs or rec.position.legs)[0]
    sym = leg.contract.symbol
    opened = rec.position.opened_at
    if opened is None:
        raise ValueError("missing opened_at")
    qty = leg.quantity_contracts
    entry_px = leg.average_entry_price.value
    entry_premium = entry_px * Decimal(qty)

    acct_actual = None
    policy = rec.position.exit_policy.model_copy(update={"time_exit": None})
    position = rec.position.model_copy(
        update={"state": TradeState.OPEN, "as_of": opened, "exit_policy": policy}
    )
    intent = rec.intent
    last_carry_day: date | None = None

    sim_exit_at: datetime | None = None
    sim_exit_detail: str | None = None
    max_loss_hit_overnight = False
    held_overnight_sim = False

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
                gross = (bid - entry_px) * Decimal(qty)
                lots = rec.risk_decision.approved_legs[0].lots.count if rec.risk_decision.approved_legs else 1
                charges = (charges_per_lot.amount * Decimal(lots) * 2).quantize(Decimal("0.01"))
                sim_net = gross - charges
                sim_exit_at = at
                sim_exit_detail = ev.detail
                if (
                    held_overnight_sim
                    and ev.kind is ExitKind.STOP
                    and "P&L stop" in ev.detail
                ):
                    max_loss_hit_overnight = True
                return SimTrade(
                    trade_id=rec.trade_id,
                    opened_at=opened,
                    closed_at=rec.as_of,
                    actual_net=Decimal(0),
                    sim_net=sim_net,
                    sim_exit_at=sim_exit_at,
                    sim_exit_detail=sim_exit_detail,
                    held_overnight_original=_held_overnight(rec),
                    held_overnight_sim=held_overnight_sim,
                    max_loss_hit_overnight=max_loss_hit_overnight,
                    entry_premium=entry_premium,
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
    )


def _held_overnight(rec: PositionLifecycleRecord) -> bool:
    opened = rec.position.opened_at
    closed = rec.as_of
    if opened is None:
        return False
    return _session_date(opened) < _session_date(closed)


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
    day_from = start
    day_to = date.fromisoformat(args.data_to_date)
    snaps = load_chain_snaps(root, symbols, day_from, day_to)

    sim_rows: list[SimTrade] = []
    actual_total = Decimal(0)
    for rec in records:
        acct = trade_accounting(rec, orders, charges_ix, charges_per_lot=cpl)
        actual_total += acct.realized_net.amount
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
                held_overnight_original=_held_overnight(rec),
                held_overnight_sim=False,
                max_loss_hit_overnight=False,
                entry_premium=entry_premium,
            )
        else:
            row = simulate_trade(rec, snaps, engine, cpl, data_end)
            row = replace(row, actual_net=acct.realized_net.amount)
        sim_rows.append(row)

    sim_total = sum((r.sim_net or Decimal(0) for r in sim_rows), Decimal(0))
    hold_hours_actual = []
    hold_hours_sim = []
    peak_capital = Decimal(0)
    eod_flatten_ids = []
    overnight_would_hold = []
    overnight_max_loss = []

    for rec, row in zip(records, sim_rows, strict=True):
        opened = rec.position.opened_at
        if opened:
            hold_hours_actual.append(
                (rec.as_of - opened).total_seconds() / 3600.0
            )
        if row.sim_exit_at and opened:
            hold_hours_sim.append((row.sim_exit_at - opened).total_seconds() / 3600.0)
        peak_capital = max(peak_capital, row.entry_premium)
        if _is_carry_flatten_close(rec):
            eod_flatten_ids.append(rec.trade_id)
            overnight_would_hold.append(rec.trade_id)
        if row.max_loss_hit_overnight:
            overnight_max_loss.append(rec.trade_id)

    cum_actual = Decimal(0)
    cum_sim = Decimal(0)
    peak_actual = Decimal(0)
    peak_sim = Decimal(0)
    trough_actual = Decimal(0)
    trough_sim = Decimal(0)
    for row in sim_rows:
        cum_actual += row.actual_net
        peak_actual = max(peak_actual, cum_actual)
        trough_actual = min(trough_actual, cum_actual - row.entry_premium)
        if row.sim_net is not None:
            cum_sim += row.sim_net
            peak_sim = max(peak_sim, cum_sim)
            trough_sim = min(trough_sim, cum_sim - row.entry_premium)

    out = {
        "period_ist": f"{start}..{end}",
        "m2_closed_in_period": len(records),
        "original_net_inr": str(actual_total.quantize(Decimal("0.01"))),
        "positional_no_eod_net_inr": str(sim_total.quantize(Decimal("0.01"))),
        "net_pnl_delta_inr": str((sim_total - actual_total).quantize(Decimal("0.01"))),
        "trade_count_delta": 0,
        "avg_hold_hours_original": round(
            sum(hold_hours_actual) / len(hold_hours_actual), 2
        )
        if hold_hours_actual
        else None,
        "avg_hold_hours_positional": round(
            sum(hold_hours_sim) / len(hold_hours_sim), 2
        )
        if hold_hours_sim
        else None,
        "peak_capital_tied_up_inr": str(peak_capital.quantize(Decimal("0.01"))),
        "max_drawdown_proxy_actual_inr": str(
            (peak_actual - trough_actual).quantize(Decimal("0.01"))
            if hold_hours_actual
            else "0"
        ),
        "max_drawdown_proxy_positional_inr": str(
            (peak_sim - trough_sim).quantize(Decimal("0.01")) if hold_hours_sim else "0"
        ),
        "eod_carry_flatten_trades": eod_flatten_ids,
        "would_hold_overnight_in_positional": overnight_would_hold,
        "overnight_layer2_stop_hits": overnight_max_loss,
        "trades": [
            {
                "trade_id": r.trade_id,
                "actual_net": str(r.actual_net),
                "sim_net": str(r.sim_net) if r.sim_net is not None else None,
                "sim_exit_detail": r.sim_exit_detail,
                "held_overnight_original": r.held_overnight_original,
                "held_overnight_sim": r.held_overnight_sim,
            }
            for r in sim_rows
        ],
    }
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
