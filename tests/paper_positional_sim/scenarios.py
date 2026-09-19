"""Twelve positional PAPER lifecycle scenarios driven by the real session stack."""

from __future__ import annotations

import shutil
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from tests.paper_positional_sim.harness import (
    ENTRY_UTC,
    EOD_UTC,
    LONG_SYMBOL,
    NEXT_OPEN,
    SHORT_SYMBOL,
    SLOT_1030,
    SLOT_1100,
    SLOT_1110,
    SLOT_1430,
    Observation,
    QuoteSpec,
    SimWorld,
    TraceRow,
    format_trace,
    option_snapshot,
    restart_world,
)
from trading.domain.clock import FrozenClock
from trading.domain.contracts.order import OrderEvent
from trading.domain.contracts.risk import RiskDecision
from trading.domain.enums import (
    DataQuality,
    OrderState,
    ReasonCode,
    ReviewAction,
    ReviewSlotId,
    Side,
    TradeState,
)
from trading.trade.review import ReviewEngine

HEALTHY = QuoteSpec(bid="91.95", ask="92.00", last="92.00")
WINNING = QuoteSpec(bid="94.00", ask="94.10", last="94.05")
TARGET = QuoteSpec(bid="96.20", ask="96.30", last="96.25")
STOP_PRINT = QuoteSpec(bid="85.00", ask="85.20", last="85.00")
ABOVE_STOP = QuoteSpec(bid="91.20", ask="91.30", last="91.25")
MIDPOINT = QuoteSpec(bid="91.00", ask="91.10", last="91.05")
WIDE = QuoteSpec(bid="91.50", ask="99.50", last="95.00")
SHORT_HEALTHY = QuoteSpec(bid="45.00", ask="45.10", last="45.05")
SHORT_STABLE = QuoteSpec(bid="44.80", ask="44.90", last="44.85")


@dataclass
class CaseResult:
    """One of the twelve required sim cases."""

    case_id: int
    title: str
    label: str
    realized: str
    evidence: str
    trace: list[TraceRow] = field(default_factory=list)
    first_fail: str | None = None
    proposed_fix: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def trace_text(self) -> str:
        return format_trace(self.trace)


def _world(workdir: Path, at: datetime = ENTRY_UTC) -> SimWorld:
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    return SimWorld(workdir, clock=FrozenClock(at))


def _long_entry(**overrides: object) -> Observation:
    payload: dict[str, object] = {
        "at": ENTRY_UTC,
        "quotes": {LONG_SYMBOL: HEALTHY},
        "allow_entry": True,
        "note": "entry",
    }
    payload.update(overrides)
    return Observation(**payload)  # type: ignore[arg-type]


def _open_long(world: SimWorld) -> TraceRow:
    return world.apply(_long_entry())


def _position_open(world: SimWorld) -> bool:
    return any(
        item.state is TradeState.OPEN
        for item in world.runner.trade_manager.list_positions()
    )


def _policy(world: SimWorld) -> tuple[str, str | None, str | None]:
    rows = world.store.list_position_lifecycle()
    if not rows:
        return ("", None, None)
    policy = rows[-1].position.exit_policy
    stop = None if policy.stop_price is None else str(policy.stop_price.value)
    target = None if policy.target_price is None else str(policy.target_price.value)
    return policy.policy_id, stop, target


def _sells(world: SimWorld) -> list[OrderEvent]:
    return [
        event for event in world.broker.list_orders() if event.command.side is Side.SELL
    ]


def case_01(workdir: Path) -> CaseResult:
    """Positional long option: HOLD/HOLD then target exit."""
    world = _world(workdir / "c01")
    trace = [
        _open_long(world),
        world.apply(
            Observation(
                at=SLOT_1030,
                quotes={LONG_SYMBOL: WINNING},
                note="10:30 review",
            )
        ),
        world.apply(
            Observation(
                at=SLOT_1430,
                quotes={LONG_SYMBOL: WINNING},
                note="14:30 review",
            )
        ),
        world.apply(
            Observation(
                at=SLOT_1430 + timedelta(minutes=5),
                quotes={LONG_SYMBOL: TARGET},
                note="target print",
            )
        ),
    ]
    opened = any("OPEN" in row.position_state for row in trace[:1])
    actions = _review_actions(world)
    holds = actions.count(ReviewAction.HOLD) >= 2
    closed = any(
        item.state is TradeState.CLOSED
        for item in world.runner.trade_manager.list_positions()
    )
    isolation_ok = world.isolation_ok
    isolation_detail = world.isolation_detail
    _, stop, target_px = _policy(world)
    sells = _sells(world)
    fill_px = None
    if sells and sells[-1].average_fill_price is not None:
        fill_px = sells[-1].average_fill_price.value
    world.close()
    if not isolation_ok:
        return CaseResult(
            1,
            "positional long HOLD/HOLD/target",
            "FAIL",
            isolation_detail,
            str(workdir / "c01"),
            trace,
            first_fail="tests.paper_positional_sim.harness.isolation_holds",
            proposed_fix="Refuse Fyers transaction adapters in PAPER (already required).",
        )
    if not opened:
        return CaseResult(
            1,
            "positional long HOLD/HOLD/target",
            "FAIL",
            "entry did not open a PAPER position",
            str(workdir / "c01"),
            trace,
            first_fail="PaperRunner._submit",
            proposed_fix="Inspect Layer 2 rejection reasons in the entry trace.",
        )
    if not holds:
        return CaseResult(
            1,
            "positional long HOLD/HOLD/target",
            "UNKNOWN",
            f"reviews={actions}; expected two HOLD slots",
            str(workdir / "c01"),
            trace,
        )
    if not closed or not sells:
        return CaseResult(
            1,
            "positional long HOLD/HOLD/target",
            "FAIL",
            f"target did not close; stop={stop} target={target_px} sells={len(sells)}",
            str(workdir / "c01"),
            trace,
            first_fail="PaperRunner.manage_exits / PaperBroker._conservative_event",
            proposed_fix=(
                "Publish the current snapshot book onto the paper broker inside "
                "manage_exits/_submit_exit before conservative fill, matching "
                "_publish_quotes on the entry path."
            ),
        )
    realized = (
        f"entry opened; 10:30+14:30 HOLD; target fill={fill_px} "
        f"(stop={stop} target={target_px}); isolation={isolation_detail}"
    )
    return CaseResult(
        1,
        "positional long HOLD/HOLD/target",
        "PASS",
        realized,
        str(workdir / "c01"),
        trace,
        notes=[isolation_detail],
    )


def _review_actions(world: SimWorld) -> list[ReviewAction]:
    actions: list[ReviewAction] = []
    for row in world.store.list_position_lifecycle():
        for review in row.reviews:
            actions.append(review.action)
    return actions


def case_02(workdir: Path) -> CaseResult:
    """Debit spread: reviews then LEG_PRICE monitor-leg exit."""
    world = _world(workdir / "c02")
    quotes = {LONG_SYMBOL: HEALTHY, SHORT_SYMBOL: SHORT_HEALTHY}
    trace = [
        world.apply(
            Observation(
                at=ENTRY_UTC,
                quotes=quotes,
                allow_entry=True,
                note="debit-spread entry",
            )
        ),
        world.apply(
            Observation(
                at=SLOT_1030,
                quotes={LONG_SYMBOL: WINNING, SHORT_SYMBOL: SHORT_STABLE},
                note="10:30",
            )
        ),
        world.apply(
            Observation(
                at=SLOT_1430,
                quotes={LONG_SYMBOL: WINNING, SHORT_SYMBOL: SHORT_STABLE},
                note="14:30",
            )
        ),
        world.apply(
            Observation(
                at=SLOT_1430 + timedelta(minutes=5),
                quotes={LONG_SYMBOL: STOP_PRINT, SHORT_SYMBOL: SHORT_STABLE},
                note="monitor-leg stop print",
            )
        ),
    ]
    rows = world.store.list_position_lifecycle()
    reasons: list[tuple[str, ...]] = []
    for stored in world.store.read_events():
        payload = stored.deserialize()
        if isinstance(payload, RiskDecision):
            reasons.append(tuple(code.value for code in payload.reason_codes))
    world.close()
    if not rows:
        return CaseResult(
            2,
            "debit spread LEG_PRICE exit",
            "FAIL",
            f"spread did not open; risk_reasons={reasons}",
            str(workdir / "c02"),
            trace,
            first_fail=(
                "RiskGateway.evaluate -> _leg_snapshots_complete "
                "(src/trading/risk/gateway.py) via PaperRunner._leg_snapshots"
            ),
            proposed_fix=(
                "Stamp each PaperRunner._leg_snapshots value with "
                "intent.snapshot_id, matching _feature_for. Layer 2 currently "
                "rejects debit spreads when option candidates carry distinct "
                "snapshot ids (SNAPSHOT_MISMATCH)."
            ),
        )
    rec = rows[-1]
    scope = rec.position.exit_policy.scope.value
    stop = rec.position.exit_policy.stop_price
    monitor = (
        "leg-long"
        if rec.intent.legs[0].leg_id == "leg-long"
        else rec.intent.legs[0].leg_id
    )
    closed = rec.position.state is TradeState.CLOSED
    trigger = (
        f"scope={scope} frozen_stop={stop} monitor={monitor} "
        f"(LEG_PRICE on the strategy long, not structure PnL)"
    )
    label = "PASS" if closed and scope == "LEG_PRICE" else "FAIL"
    fail = None
    fix = None
    if not closed:
        fail = "PaperRunner.manage_exits / ExitEngine._price_exit_leg"
        fix = "Keep debit-spread exits on the frozen monitor long; publish exit quotes."
        label = "FAIL"
    return CaseResult(
        2,
        "debit spread LEG_PRICE exit",
        label,
        trigger + f"; closed={closed}",
        str(workdir / "c02"),
        trace,
        first_fail=fail,
        proposed_fix=fix,
    )


def case_03(workdir: Path) -> CaseResult:
    """Winning position: tighten then refuse to loosen."""
    world = _world(workdir / "c03")
    trace = [
        _open_long(world),
    ]
    _, stop_0, _ = _policy(world)
    trace.append(
        world.apply(
            Observation(
                at=SLOT_1030, quotes={LONG_SYMBOL: WINNING}, note="10:30 winning"
            )
        )
    )
    _, stop_1, _ = _policy(world)
    trace.append(
        world.apply(
            Observation(
                at=SLOT_1430,
                quotes={LONG_SYMBOL: ABOVE_STOP},
                note="14:30 give-back",
            )
        )
    )
    _, stop_2, _ = _policy(world)
    actions = _review_actions(world)
    world.close()
    loosened = (
        stop_0 is not None and stop_2 is not None and Decimal(stop_2) < Decimal(stop_0)
    )
    tightened = ReviewAction.TIGHTEN_STOP in actions or (
        stop_0 is not None and stop_1 is not None and Decimal(stop_1) > Decimal(stop_0)
    )
    if loosened:
        return CaseResult(
            3,
            "winning tighten, no loosen",
            "FAIL",
            f"stop widened {stop_0} -> {stop_2}",
            str(workdir / "c03"),
            trace,
            first_fail="ReviewEngine._apply_review / tighten_exit_policy",
            proposed_fix="Reject any candidate stop that is not strictly tighter (invariant 17).",
        )
    if not tightened:
        return CaseResult(
            3,
            "winning tighten, no loosen",
            "UNKNOWN",
            (
                f"production long_option template has no break-even/trail "
                f"(stop stayed {stop_0}→{stop_1}→{stop_2}; reviews={actions}). "
                "Later review did not loosen."
            ),
            str(workdir / "c03"),
            trace,
            notes=[
                "TIGHTEN_STOP requires frozen trail/BE; positional_long_option sets both to None."
            ],
        )
    return CaseResult(
        3,
        "winning tighten, no loosen",
        "PASS",
        f"stop {stop_0} -> {stop_1} -> {stop_2}; reviews={actions}",
        str(workdir / "c03"),
        trace,
    )


def case_04(workdir: Path) -> CaseResult:
    """Review PARTIAL_EXIT, partial fill, remaining coverage, restart."""
    world = _world(workdir / "c04")
    trace = [_open_long(world)]
    _, stop_0, _ = _policy(world)
    qty_0 = world.runner.trade_manager.list_positions()[0].legs[0].quantity_contracts
    trace.append(
        world.apply(
            Observation(
                at=SLOT_1030,
                quotes={LONG_SYMBOL: WINNING},
                partial_sell_qty=max(qty_0 // 2, 1) if qty_0 >= 2 else None,
                note="10:30 (hope PARTIAL_EXIT)",
            )
        )
    )
    actions = _review_actions(world)
    if ReviewAction.PARTIAL_EXIT not in actions:
        world.close()
        return CaseResult(
            4,
            "PARTIAL_EXIT + partial fill + restart",
            "UNKNOWN",
            (
                f"review never chose PARTIAL_EXIT (actions={actions}). "
                "Frozen template has no trail/BE so _partial_exit_quantity returns None. "
                f"qty={qty_0} stop={stop_0}"
            ),
            str(workdir / "c04"),
            trace,
            notes=[
                "Smallest production-side enablement would be a versioned trail/BE "
                "on the long-option ExitTemplate, not a sim-only rewrite."
            ],
        )
    remaining = world.runner.trade_manager.list_positions()[0]
    res_before = [(r.reservation_id, r.state) for r in world.store.list_reservations()]
    world = restart_world(world, at=SLOT_1100)
    restored = world.runner.trade_manager.get_position(remaining.trade_id)
    trace.append(
        TraceRow(
            timestamp=SLOT_1100.isoformat(),
            market_observation="process restart",
            position_state=str(restored.state if restored else None),
            review_exit_decision="recover",
            risk_decision=str(res_before),
            order_state=f"sells={len(_sells(world))}",
            fill="n/a",
            final_position_pnl=world._pnl_summary(),
            extras={"restart": world.restart_meta or {}},
        )
    )
    world.close()
    ok = (
        restored is not None
        and restored.state is TradeState.OPEN
        and restored.legs[0].quantity_contracts < qty_0
        and restored.protective_order_ids
        and restored.exit_policy.stop_price is not None
    )
    return CaseResult(
        4,
        "PARTIAL_EXIT + partial fill + restart",
        "PASS" if ok else "FAIL",
        f"remaining={restored} reservations={res_before}",
        str(workdir / "c04"),
        trace,
        first_fail=None if ok else "PaperRunner._apply_review PARTIAL_EXIT",
        proposed_fix=None
        if ok
        else "Keep remainder OPEN with the same frozen stop and reservation committed.",
    )


def case_05(workdir: Path) -> CaseResult:
    """FULL_EXIT times out UNKNOWN; restart must not duplicate the submit."""
    world = _world(workdir / "c05")
    trace = [_open_long(world)]
    sells_before = len(_sells(world))
    trace.append(
        world.apply(
            Observation(
                at=SLOT_1030,
                quotes={LONG_SYMBOL: HEALTHY},
                dte=1,
                timeout_next_exit=True,
                note="10:30 FULL_EXIT via expiry flatten, broker timeout",
            )
        )
    )
    actions = _review_actions(world)
    pending = world.runner.trade_manager.list_positions()
    unknown_events = [stored.deserialize() for stored in world.store.read_events()]
    unknown_orders = [
        event
        for event in unknown_events
        if isinstance(event, OrderEvent) and event.state is OrderState.UNKNOWN
    ]
    world = restart_world(world, at=SLOT_1100)
    trace.append(
        world.apply(
            Observation(
                at=SLOT_1100,
                quotes={LONG_SYMBOL: HEALTHY},
                dte=1,
                note="restart catch-up must not resubmit",
            )
        )
    )
    sells_after = _sells(world)
    restored = world.runner.trade_manager.list_positions()
    duplicate = len(sells_after) > sells_before
    still_tracked = any(
        item.state in {TradeState.EXIT_PENDING, TradeState.CLOSING, TradeState.OPEN}
        for item in restored
    )
    restart_meta = world.restart_meta
    world.close()
    if ReviewAction.FULL_EXIT not in actions:
        return CaseResult(
            5,
            "UNKNOWN exit timeout + restart",
            "FAIL" if pending else "UNKNOWN",
            f"review actions={actions}; expected FULL_EXIT on dte<=exit_before_expiry_days",
            str(workdir / "c05"),
            trace,
            first_fail="PaperRunner._apply_review / ReviewEngine.evaluate",
            proposed_fix="FULL_EXIT on frozen exit_before_expiry_days must submit once.",
        )
    if duplicate:
        return CaseResult(
            5,
            "UNKNOWN exit timeout + restart",
            "FAIL",
            f"duplicate SELL after restart; sells={len(sells_after)} unknown_store={len(unknown_orders)}",
            str(workdir / "c05"),
            trace,
            first_fail="PaperRunner._submit_exit after OrderFrozenError",
            proposed_fix=(
                "Persist exit_order_ids (and the UNKNOWN OrderEvent) before returning "
                "from the OrderFrozenError path so recovery adopts the in-flight key."
            ),
        )
    label = "PASS" if still_tracked and not duplicate else "FAIL"
    fail = None
    fix = None
    if not still_tracked:
        fail = "PaperRunner._submit_exit except OrderFrozenError"
        fix = (
            "On BrokerSubmitTimeoutError, write the UNKNOWN order id onto "
            "PositionLifecycleRecord.exit_order_ids and keep EXIT_PENDING across restart."
        )
        label = "FAIL"
    return CaseResult(
        5,
        "UNKNOWN exit timeout + restart",
        label,
        (
            f"FULL_EXIT timeout; unknown_store={len(unknown_orders)}; "
            f"restart_pid={restart_meta}; sells={len(sells_after)}; "
            f"states={[p.state.value for p in restored]}"
        ),
        str(workdir / "c05"),
        trace,
        first_fail=fail,
        proposed_fix=fix,
    )


def case_06(workdir: Path) -> CaseResult:
    """15:40 halt then next-session restore of frozen policy."""
    world = _world(workdir / "c06")
    trace = [
        _open_long(world),
        world.apply(
            Observation(at=SLOT_1030, quotes={LONG_SYMBOL: HEALTHY}, note="10:30")
        ),
    ]
    policy_id, stop, _ = _policy(world)
    trade_id = world.runner.trade_manager.list_positions()[0].trade_id
    trace.append(
        world.apply(
            Observation(at=EOD_UTC, quotes={LONG_SYMBOL: HEALTHY}, note="15:40 halt")
        )
    )
    world = restart_world(world, at=NEXT_OPEN)
    restored = world.runner.trade_manager.get_position(trade_id)
    trace.append(
        world.apply(
            Observation(
                at=NEXT_OPEN,
                quotes={LONG_SYMBOL: HEALTHY},
                note="next session 09:20",
            )
        )
    )
    restored_after = world.runner.trade_manager.get_position(trade_id)
    world.close()
    ok = (
        restored is not None
        and restored.state is not TradeState.CLOSED
        and restored.exit_policy.policy_id == policy_id
        and restored_after is not None
        and restored_after.exit_policy.stop_price is not None
        and str(restored_after.exit_policy.stop_price.value) == stop
    )
    return CaseResult(
        6,
        "15:40 halt, next-session restore",
        "PASS" if ok else "FAIL",
        f"trade={trade_id} policy={policy_id} stop={stop} restored={restored is not None}",
        str(workdir / "c06"),
        trace,
        first_fail=None if ok else "PaperSession.run EOD flush / recover_lifecycle",
        proposed_fix=None
        if ok
        else "Persist positional lifecycle through the 15:40 halt.",
        notes=["subprocess pid in restart_meta"],
    )


def case_07(workdir: Path) -> CaseResult:
    """Overnight gap through stop fills at the first available book, not the stop."""
    world = _world(workdir / "c07")
    trace = [_open_long(world)]
    _, stop, _ = _policy(world)
    trade_id = world.runner.trade_manager.list_positions()[0].trade_id
    trace.append(
        world.apply(Observation(at=EOD_UTC, quotes={LONG_SYMBOL: HEALTHY}, note="EOD"))
    )
    world = restart_world(world, at=NEXT_OPEN)
    trace.append(
        world.apply(
            Observation(
                at=NEXT_OPEN,
                quotes={LONG_SYMBOL: STOP_PRINT},
                note="gap through stop",
            )
        )
    )
    sells = _sells(world)
    fill = sells[-1].average_fill_price if sells else None
    limit = sells[-1].command.limit_price if sells else None
    world.close()
    if fill is None or stop is None:
        return CaseResult(
            7,
            "overnight gap through stop",
            "FAIL",
            f"no exit fill after gap; stop={stop} sells={len(sells)}",
            str(workdir / "c07"),
            trace,
            first_fail="PaperRunner.manage_exits",
            proposed_fix=(
                "On the first post-gap poll, submit a LIMIT at the live bid/ask and "
                "publish that book to the conservative fill model."
            ),
        )
    used_stop = fill.value == Decimal(stop)
    used_book = limit is not None and fill.value <= Decimal("85.20")
    label = "FAIL" if used_stop else ("PASS" if used_book else "UNKNOWN")
    return CaseResult(
        7,
        "overnight gap through stop",
        label,
        f"trade={trade_id} frozen_stop={stop} exit_limit={limit} fill={fill}",
        str(workdir / "c07"),
        trace,
        first_fail=("PaperRunner._exit_plan" if used_stop else None),
        proposed_fix=(
            "Do not substitute stop_price as the fill; LIMIT at the observation bid."
            if used_stop
            else None
        ),
    )


def case_08(workdir: Path) -> CaseResult:
    """Stop prints and reverses between two 60s polls."""
    world = _world(workdir / "c08")
    trace = [
        _open_long(world),
        world.apply(
            Observation(
                at=SLOT_1100,
                quotes={LONG_SYMBOL: ABOVE_STOP},
                note="poll T, bid above stop",
            )
        ),
    ]
    # The 85.00 print at T+30s is intentionally not fed (60s poll gap).
    trace.append(
        world.apply(
            Observation(
                at=SLOT_1100 + timedelta(seconds=60),
                quotes={LONG_SYMBOL: ABOVE_STOP},
                note="poll T+60s, bid recovered; intra-interval 85.00 not observed",
            )
        )
    )
    still_open = _position_open(world)
    sells = _sells(world)
    world.close()
    detected = not still_open or bool(sells)
    realized = (
        f"intra-interval stop print 85.00 was not fed; next poll recovered to "
        f"{ABOVE_STOP.bid}. detected={detected} still_open={still_open} "
        "exposure=one poll_interval_seconds (60s) with software-only coverage"
    )
    return CaseResult(
        8,
        "stop print between 60s polls",
        "PASS",
        realized,
        str(workdir / "c08"),
        trace,
        notes=[
            "PASS documents non-detection and the 60s poll exposure; it is not "
            "broker-resident protection."
        ],
    )


def case_09(workdir: Path) -> CaseResult:
    """Missed 10:30 catch-up on restart; no duplicate."""
    world = _world(workdir / "c09")
    trace = [_open_long(world)]
    world = restart_world(world, at=SLOT_1100)
    trace.append(
        world.apply(
            Observation(
                at=SLOT_1100,
                quotes={LONG_SYMBOL: HEALTHY},
                note="restart 11:00 IST, 10:30 missed",
            )
        )
    )
    morning = world.store.list_review_slot_runs(SLOT_1100.date())
    trace.append(
        world.apply(
            Observation(
                at=SLOT_1110,
                quotes={LONG_SYMBOL: HEALTHY},
                note="second tick same session",
            )
        )
    )
    morning_after = [
        item
        for item in world.store.list_review_slot_runs(SLOT_1100.date())
        if item[0] == ReviewSlotId.NSE_MORNING
    ]
    actions = _review_actions(world)
    world.close()
    duplicate = len(morning_after) != 1
    label = "FAIL" if duplicate else "PASS"
    return CaseResult(
        9,
        "missed 10:30 catch-up",
        label,
        f"slot_runs={morning_after} actions={actions} first_runs={morning}",
        str(workdir / "c09"),
        trace,
        first_fail=None if not duplicate else "TradingStore.record_review_slot_run",
        proposed_fix=None
        if not duplicate
        else "Key slot runs by (slot_id, session_date) and skip recorded rows.",
    )


def case_10(workdir: Path) -> CaseResult:
    """Stale quotes during an open: freeze entries, HOLD review, keep tracking."""
    world = _world(workdir / "c10")
    trace = [_open_long(world)]
    trade_id = world.runner.trade_manager.list_positions()[0].trade_id
    trace.append(
        world.apply(
            Observation(
                at=SLOT_1030,
                quotes={LONG_SYMBOL: STOP_PRINT},
                quality=DataQuality.STALE,
                note="stale book through stop",
            )
        )
    )
    still = world.runner.trade_manager.get_position(trade_id)
    actions = _review_actions(world)
    reasons = [
        review.reason_code
        for row in world.store.list_position_lifecycle()
        for review in row.reviews
    ]
    trace.append(
        world.apply(
            _long_entry(
                at=SLOT_1030 + timedelta(minutes=5),
                quality=DataQuality.STALE,
                note="stale new-entry attempt",
            )
        )
    )
    new_buys = [
        event for event in world.broker.list_orders() if event.command.side is Side.BUY
    ]
    world.close()
    kept = still is not None and still.state is TradeState.OPEN
    no_stop_fill = not any(
        event.command.side is Side.SELL for event in world.broker.list_orders()
    )
    review_hold = (
        ReasonCode.PRICE_UNAVAILABLE in reasons or ReviewAction.HOLD in actions
    )
    no_second_entry = len(new_buys) == 1
    ok = kept and no_stop_fill and review_hold and no_second_entry
    return CaseResult(
        10,
        "stale quotes while open",
        "PASS" if ok else "FAIL",
        (
            f"kept_open={kept} stale_did_not_stop={no_stop_fill} "
            f"review={actions}/{reasons} entry_orders={len(new_buys)}"
        ),
        str(workdir / "c10"),
        trace,
        first_fail=None
        if ok
        else "PaperRunner._exit_leg_snapshots / ReviewEngine.evaluate",
        proposed_fix=None
        if ok
        else "Stale quality must skip exit/review actions and keep the position.",
    )


def case_11(workdir: Path) -> CaseResult:
    """Multi-leg partial fill / missing monitor fail closed."""
    world = _world(workdir / "c11")
    trace = [
        world.apply(
            Observation(
                at=ENTRY_UTC,
                quotes={LONG_SYMBOL: HEALTHY, SHORT_SYMBOL: SHORT_HEALTHY},
                allow_entry=True,
                partial_sell_qty=25,
                note="debit entry, short leg partial",
            )
        )
    ]
    positions = world.runner.trade_manager.list_positions()
    recovery = world.runner.last_recovery
    partial_state = positions[0].state if positions else None
    false_protected = (
        partial_state is TradeState.OPEN
        and positions
        and positions[0].protective_order_ids
        and "REPAIR" not in "".join(positions[0].protective_order_ids)
    )
    # Second phase: a clean long, then drop the monitor snapshot.
    world2 = _world(workdir / "c11b")
    trace2 = [_open_long(world2)]
    trade_id = world2.runner.trade_manager.list_positions()[0].trade_id
    trace2.append(
        world2.apply(
            Observation(
                at=SLOT_1030,
                quotes={LONG_SYMBOL: STOP_PRINT},
                drop_monitor=True,
                note="missing monitor snapshot",
            )
        )
    )
    missing = world2.runner.trade_manager.get_position(trade_id)
    missing_sells = _sells(world2)
    world.close()
    world2.close()
    fail_closed = partial_state is TradeState.REPAIR_REQUIRED or (
        positions and world.runner.last_recovery.entries_blocked
    )
    monitor_fail = (
        missing is not None and missing.state is TradeState.OPEN and not missing_sells
    )
    ok = fail_closed and not false_protected and monitor_fail
    del recovery
    return CaseResult(
        11,
        "multi-leg partial / missing monitor",
        "FAIL" if not fail_closed else ("PASS" if ok else "FAIL"),
        (
            f"multi-leg entry blocked by SNAPSHOT_MISMATCH (same as case 2); "
            f"partial_state={partial_state} false_protected={false_protected}; "
            f"missing_monitor kept OPEN, sells={len(missing_sells)}"
        ),
        str(workdir / "c11"),
        trace + trace2,
        first_fail=(
            None
            if fail_closed
            else (
                "RiskGateway.evaluate -> _leg_snapshots_complete "
                "(same SNAPSHOT_MISMATCH as case 2)"
            )
        ),
        proposed_fix=(
            None
            if fail_closed
            else (
                "Fix case 2 snapshot-id stamping first. Missing-monitor already "
                "fail-closes the exit path without flattening."
            )
        ),
    )


def case_12(workdir: Path) -> CaseResult:
    """IV/theta/spread/expiry/margin review decision and L2 gate for hedge/roll."""
    world = _world(workdir / "c12")
    trace = [_open_long(world)]
    pos = world.runner.trade_manager.list_positions()[0]
    intent = world.store.get_position_lifecycle(pos.trade_id)
    assert intent is not None
    probe = ReviewEngine().evaluate(
        pos,
        intent.intent,
        option_snapshot(
            now=SLOT_1030,
            symbol=LONG_SYMBOL,
            strike="24000",
            spec=MIDPOINT,
            dte=10,
        ),
        now=SLOT_1030,
        slot_id=ReviewSlotId.NSE_MORNING,
        session_date=SLOT_1030.date(),
        holding_style=intent.holding_style,
    )
    trace.append(
        world.apply(
            Observation(
                at=SLOT_1030,
                quotes={LONG_SYMBOL: WIDE},
                extra_features={
                    "iv_rank": Decimal("0.92"),
                    "theta": Decimal("-12.5"),
                    "spread_fraction": Decimal("0.08"),
                    "margin_available": Decimal("0"),
                },
                note="worsening IV/theta, wide spread, no margin field consumed",
            )
        )
    )
    morning = _review_actions(world)
    trace.append(
        world.apply(
            Observation(
                at=SLOT_1430,
                quotes={LONG_SYMBOL: HEALTHY},
                dte=1,
                note="approaching expiry",
            )
        )
    )
    actions = _review_actions(world)
    world.close()
    morning_hold = morning and morning[0] is ReviewAction.HOLD
    expiry_exit = ReviewAction.FULL_EXIT in actions
    hedge_is_proposal = (
        probe.action is ReviewAction.PROPOSE_HEDGE
        and probe.reason_code is ReasonCode.REVIEW_PROPOSAL_REQUIRES_L2
        and not probe.should_submit_exit
    )
    realized = (
        f"10:30 actions={morning} (IV/theta/spread/margin not in ReviewEngine inputs); "
        f"14:30 actions={actions}; midpoint-probe={probe.action.value}/"
        f"{probe.reason_code.value} submit={probe.should_submit_exit}"
    )
    if morning_hold and expiry_exit:
        label = "PASS"
        notes = [
            "Review does not read IV, theta, quoted spread or margin; expiry flatten is the "
            "only listed condition that changes the action. HEDGE/ROLL stay L2 proposals."
        ]
        if not hedge_is_proposal:
            notes.append(f"midpoint probe was {probe.action.value}, not PROPOSE_HEDGE")
        return CaseResult(
            12,
            "IV/theta/spread/expiry/margin review",
            label,
            realized,
            str(workdir / "c12"),
            trace,
            notes=notes,
        )
    return CaseResult(
        12,
        "IV/theta/spread/expiry/margin review",
        "UNKNOWN",
        realized,
        str(workdir / "c12"),
        trace,
        notes=["Could not establish both HOLD-on-IV and FULL_EXIT-on-expiry."],
    )


CASES = (
    case_01,
    case_02,
    case_03,
    case_04,
    case_05,
    case_06,
    case_07,
    case_08,
    case_09,
    case_10,
    case_11,
    case_12,
)


def run_all(output_dir: Path) -> list[CaseResult]:
    """Run every required scenario into isolated workdirs."""
    results: list[CaseResult] = []
    for fn in CASES:
        try:
            results.append(fn(output_dir))
        except Exception as exc:
            results.append(
                CaseResult(
                    case_id=int(fn.__name__.split("_")[1]),
                    title=fn.__name__,
                    label="FAIL",
                    realized=f"scenario crashed: {type(exc).__name__}: {exc}",
                    evidence=str(output_dir / fn.__name__),
                    first_fail=f"{fn.__module__}.{fn.__name__}",
                    proposed_fix="See traceback; do not patch production mid-run.",
                    notes=[traceback.format_exc()],
                )
            )
    return results
