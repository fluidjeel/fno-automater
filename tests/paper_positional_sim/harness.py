"""Deterministic PAPER session harness. Market, broker and clock are fixtures.

Production PaperSession, PaperRunner, Layer 3, Layer 2, TradeManager and OMS
are used as-is. Quotes are fed in timestamp order. Restart is a subprocess.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import tests.factories as f
from trading.broker.paper import PaperBroker
from trading.broker.paper.fixtures import PaperBrokerFixtures
from trading.broker.ports import (
    BrokerFunds,
    BrokerSubmitRequest,
    BrokerSubmitTimeoutError,
)
from trading.config import load_config, load_evaluation_config, load_risk_policy
from trading.config.schema import Environment
from trading.domain.clock import FrozenClock
from trading.domain.contracts import (
    DerivativesContext,
    FeatureSnapshot,
    InstrumentSpec,
    OrderEvent,
)
from trading.domain.contracts.snapshot import MarketQuote
from trading.domain.enums import (
    DataQuality,
    ExecutionMode,
    OptionType,
    OrderState,
    ReasonCode,
    Side,
    TradeState,
)
from trading.domain.ids import SequentialIdFactory
from trading.domain.primitives import Currency, Money, Price
from trading.runtime.isolation import PaperIsolationError, assert_paper_isolation
from trading.runtime.paper_runner import PaperRunner, PaperStrategyRequest
from trading.runtime.paper_session import PaperSession, load_paper_session_config
from trading.runtime.protection import build_protection_coordinator
from trading.runtime.rest_quote_monitor import RestQuoteMonitor
from trading.storage.trading_store import TradingStore
from trading.strategies.macro import MacroAssessment, MacroBias
from trading.trade.exits import monitor_leg, tighten_exit_policy

ROOT = Path(__file__).resolve().parents[2]
IST = ZoneInfo("Asia/Kolkata")
WORKER = Path(__file__).with_name("restart_worker.py")

ENTRY_UTC = datetime(2026, 9, 14, 3, 50, tzinfo=UTC)  # 09:20 IST
SLOT_1030 = datetime(2026, 9, 14, 5, 0, tzinfo=UTC)
SLOT_1100 = datetime(2026, 9, 14, 5, 30, tzinfo=UTC)
SLOT_1110 = datetime(2026, 9, 14, 5, 40, tzinfo=UTC)
SLOT_1430 = datetime(2026, 9, 14, 9, 0, tzinfo=UTC)
EOD_UTC = datetime(2026, 9, 14, 10, 10, tzinfo=UTC)
NEXT_OPEN = datetime(2026, 9, 15, 3, 50, tzinfo=UTC)  # 09:20 IST next session

LONG_SYMBOL = "NIFTY26SEP24000CE"
SHORT_SYMBOL = "NIFTY26SEP24100CE"
DEPTH = 5_000


@dataclass(frozen=True, slots=True)
class QuoteSpec:
    """One book observation for a symbol."""

    bid: str
    ask: str
    last: str | None = None
    bid_size: int = DEPTH
    ask_size: int = DEPTH


@dataclass(frozen=True, slots=True)
class Observation:
    """One in-order market observation. Later ticks never leak earlier."""

    at: datetime
    quotes: Mapping[str, QuoteSpec]
    dte: int = 10
    quality: DataQuality = DataQuality.VALID
    extra_features: Mapping[str, Decimal] = field(default_factory=dict)
    allow_entry: bool = False
    timeout_next_exit: bool = False
    partial_sell_qty: int | None = None
    drop_monitor: bool = False
    note: str = ""


@dataclass
class TraceRow:
    """One chronological sim step in the required report format."""

    timestamp: str
    market_observation: str
    position_state: str
    review_exit_decision: str
    risk_decision: str
    order_state: str
    fill: str
    final_position_pnl: str
    extras: dict[str, object] = field(default_factory=dict)


class ScriptedPaperBroker(PaperBroker):
    """Paper broker with injectable timeout and partial-fill broker inputs."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.timeout_next_exit = False
        self.partial_sell_qty: int | None = None

    def submit(self, request: BrokerSubmitRequest) -> OrderEvent:
        command = request.order.command
        if self.timeout_next_exit and command.side is Side.SELL:
            self.timeout_next_exit = False
            raise BrokerSubmitTimeoutError(request.order.identity.idempotency_key)
        if (
            self.partial_sell_qty is not None
            and command.side is Side.SELL
            and 0 < self.partial_sell_qty < command.quantity_contracts
        ):
            qty = self.partial_sell_qty
            self.partial_sell_qty = None
            reduced_command = command.model_copy(update={"quantity_contracts": qty})
            reduced_order = request.order.model_copy(
                update={"command": reduced_command}
            )
            reduced = request.model_copy(update={"order": reduced_order})
            event = super().submit(reduced)
            patched = event.model_copy(
                update={
                    "command": command,
                    "state": OrderState.PARTIAL,
                    "filled_quantity": qty,
                    "acknowledged_quantity": qty,
                }
            )
            self._state.orders_by_internal_id[event.identity.internal_order_id] = (
                patched
            )
            self._state.orders_by_idempotency_key[event.identity.idempotency_key] = (
                patched
            )
            return patched
        return super().submit(request)


class _Sink:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def send(self, text: str) -> bool:
        self.messages.append(text)
        return True


def option_spec(symbol: str, strike: str) -> InstrumentSpec:
    return InstrumentSpec.model_validate(
        {
            "trading_symbol": symbol,
            "exchange": "NFO",
            "segment": "NSE_FO",
            "underlying": "NIFTY",
            "instrument_kind": "OPTION",
            "provider_token": "1",
            "exchange_token": 1,
            "lot_size": 75,
            "tick_size": Decimal("0.05"),
            "price_precision": 2,
            "expiry": "2026-09-24",
            "strike": Decimal(strike),
            "option_type": "CALL",
            "trading_session": "0915-1540",
            "source": "fixture",
            "verified_at": "2026-09-11",
        }
    )


def _quote(spec: QuoteSpec) -> MarketQuote:
    last = spec.last if spec.last is not None else spec.bid
    return f.quote(
        bid=f.price(spec.bid),
        ask=f.price(spec.ask),
        last=f.price(last),
        bid_size=spec.bid_size,
        ask_size=spec.ask_size,
    )


def _stamp_times(now: datetime) -> object:
    calc = now - timedelta(seconds=1)
    return f.snapshot_times(
        event_time=calc,
        source_time=calc,
        receive_time=calc + timedelta(milliseconds=50),
        calculation_time=calc + timedelta(milliseconds=120),
    )


def option_snapshot(
    *,
    now: datetime,
    symbol: str,
    strike: str,
    spec: QuoteSpec,
    dte: int,
    quality: DataQuality = DataQuality.VALID,
    extra_features: Mapping[str, Decimal] | None = None,
) -> FeatureSnapshot:
    features: dict[str, Decimal] = {"realized_vol_20d": Decimal("0.14")}
    if extra_features:
        features.update(extra_features)
    reason = (ReasonCode.DATA_STALE,) if quality is DataQuality.STALE else ()
    return f.snapshot(
        snapshot_id=f"SNAP-{symbol}-{int(now.timestamp())}",
        contract=f.option_contract(
            symbol=symbol,
            strike=Decimal(strike),
            option_type=OptionType.CALL,
        ),
        market=_quote(spec),
        times=_stamp_times(now),
        quality=f.quality(state=quality, reason_codes=reason),
        features=features,
        derivatives=DerivativesContext(
            days_to_expiry=dte,
            open_interest=5000,
            option_type=OptionType.CALL,
            underlying_price=f.price("24050"),
        ),
    )


def underlying_snapshot(
    *, now: datetime, quality: DataQuality = DataQuality.VALID
) -> FeatureSnapshot:
    reason = (ReasonCode.DATA_STALE,) if quality is DataQuality.STALE else ()
    return f.snapshot(
        snapshot_id=f"SNAP-NIFTY-{int(now.timestamp())}",
        contract=f.index_contract(),
        market=f.quote(last=f.price("24050"), close=f.price("24000")),
        times=_stamp_times(now),
        quality=f.quality(state=quality, reason_codes=reason),
    )


def isolation_holds(broker: object) -> tuple[bool, str]:
    """PAPER mode must refuse a Fyers-shaped transaction adapter."""
    try:
        assert_paper_isolation(Environment.PAPER, ExecutionMode.PAPER, broker)
    except PaperIsolationError as exc:
        return False, f"paper broker refused: {exc}"

    class _FyersShaped:
        __module__ = "trading.broker.fyers.adapter"

    try:
        assert_paper_isolation(Environment.PAPER, ExecutionMode.PAPER, _FyersShaped())
    except PaperIsolationError:
        return True, "PAPER isolation refused a Fyers transaction adapter"
    return False, "PAPER isolation allowed a Fyers-shaped adapter"


class SimWorld:
    """One PAPER process: store, broker, runner and session on a frozen clock."""

    def __init__(self, workdir: Path, *, clock: FrozenClock) -> None:
        self.workdir = workdir
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self.store_path = workdir / "trading.sqlite"
        self.broker_path = workdir / "broker_state.json"
        self.sink = _Sink()
        self.store = TradingStore.open(self.store_path, clock=clock)
        evaluation = load_evaluation_config(ROOT / "config" / "evaluation.yaml")
        risk = load_risk_policy(ROOT / "config" / "risk.yaml")
        session_cfg = load_paper_session_config(ROOT / "config" / "paper_session.yaml")
        account = load_config(
            ROOT / "config" / "paper.yaml", expect_environment=Environment.PAPER
        )
        ids = SequentialIdFactory(clock.instant)
        broker_funds = BrokerFunds(
            account_id=account.config.account_id,
            as_of=clock.now_utc(),
            equity=Money.of("700000", Currency.INR),
            margin_used=Money.zero(Currency.INR),
            margin_available=Money.of("700000", Currency.INR),
        )
        self.broker = ScriptedPaperBroker(
            clock=clock,
            id_factory=ids,
            fixtures=PaperBrokerFixtures(
                account=broker_funds,
                positions=(),
                margin_previews={},
            ),
            fill_model=evaluation.config.fill_model,
            synthetic_margin=True,
            future_margin_fraction=risk.config.paper_future_margin_fraction,
        )
        if self.broker_path.is_file():
            self.broker.load_state(
                json.loads(self.broker_path.read_text(encoding="utf-8"))
            )
        self.fill_model = evaluation.config.fill_model
        self.runner = PaperRunner(
            account_config=account,
            risk_policy=risk,
            store=self.store,
            broker=self.broker,
            clock=clock,
            id_factory=ids,
            fill_model=evaluation.config.fill_model,
            execution_mode=ExecutionMode.PAPER,
        )
        self._pending_obs: Observation | None = None
        rest = RestQuoteMonitor(clock, lambda _symbols: {})
        self.coordinator = build_protection_coordinator(
            runner=self.runner,
            clock=clock,
            config=session_cfg.protection,
            repo_root=ROOT,
            rest_fetch=rest,
            ws=None,
        )
        self.session = PaperSession(
            runner=self.runner,
            clock=clock,
            session_config=session_cfg,
            session_hours=(time(9, 15), time(15, 30)),
            timezone=IST,
            notifier=self.sink,
            request_builder=self._builder,
            observation_start=clock.now_utc(),
            capital_limit=Money.of("700000", Currency.INR),
            risk_policy_version=risk.config.policy_version,
            fill_model_version=evaluation.config.fill_model.version,
            code_version=account.version,
            charges_verified=evaluation.config.fill_model.charges_per_lot.is_verified,
            cohort_dir=workdir / "cohorts",
            broker_state_path=self.broker_path,
            sleeper=lambda _s: None,
            coordinator=self.coordinator,
        )
        self.isolation_ok, self.isolation_detail = isolation_holds(self.broker)
        self.runner.recover_lifecycle()
        self.coordinator.start()
        self.restart_meta: dict[str, object] | None = None

    def overlay_exit_template(
        self,
        *,
        break_even_trigger_ticks: int,
        trailing_activation_ticks: int,
        trailing_distance_ticks: int,
        activate_breakeven: bool = False,
    ) -> None:
        """Sim-only overlay of BE/trail onto a frozen open. Production templates stay None."""
        positions = self.runner.trade_manager.list_positions()
        if not positions:
            raise AssertionError("overlay_exit_template requires an open position")
        position = positions[0]
        book = self.runner._open_book.get(position.trade_id)
        if book is None:
            raise AssertionError("open book missing for overlay")
        intent, decision = book
        template = intent.exit_template.model_copy(
            update={
                "break_even_trigger_ticks": break_even_trigger_ticks,
                "trailing_activation_ticks": trailing_activation_ticks,
                "trailing_distance_ticks": trailing_distance_ticks,
            }
        )
        updated_intent = intent.model_copy(update={"exit_template": template})
        policy = position.exit_policy
        if activate_breakeven:
            watched = monitor_leg(updated_intent)
            leg = next(item for item in position.legs if item.leg_id == watched.leg_id)
            trigger = Price.snap(
                leg.average_entry_price.value
                + leg.average_entry_price.tick.value * break_even_trigger_ticks,
                leg.average_entry_price.tick,
            )
            tightened = tighten_exit_policy(
                policy,
                template,
                entry_price=leg.average_entry_price,
                monitor_price=trigger,
            )
            if tightened is not None:
                policy = tightened
        updated_position = position.model_copy(update={"exit_policy": policy})
        self.runner.trade_manager.restore_position(updated_position)
        self.runner._open_book[position.trade_id] = (updated_intent, decision)
        self.runner._write_lifecycle(position.trade_id)

    def close(self) -> None:
        self._persist()
        self.store.close()

    def _persist(self) -> None:
        self.runner.flush_lifecycle()
        self.broker_path.write_text(
            json.dumps(self.broker.dump_state()), encoding="utf-8"
        )

    def _builder(
        self, now: datetime
    ) -> tuple[tuple[PaperStrategyRequest, ...], dict[str, FeatureSnapshot]]:
        obs = self._pending_obs
        if obs is None:
            return (), {}
        snapshots = self.snapshots_for(obs, now)
        if not obs.allow_entry or self._has_nifty_exposure():
            return (), snapshots
        strategy_id = (
            "debit_spread" if SHORT_SYMBOL in obs.quotes else "positional_long_option"
        )
        long_snap = snapshots[LONG_SYMBOL]
        candidates: tuple[FeatureSnapshot, ...] = (long_snap,)
        instruments = {LONG_SYMBOL: option_spec(LONG_SYMBOL, "24000")}
        if SHORT_SYMBOL in snapshots:
            candidates = (long_snap, snapshots[SHORT_SYMBOL])
            instruments[SHORT_SYMBOL] = option_spec(SHORT_SYMBOL, "24100")
        request = PaperStrategyRequest(
            strategy_id=strategy_id,
            underlying=snapshots["NIFTY"],
            candidates=candidates,
            instruments=instruments,
            event_risk_state=f.event_risk_state(
                as_of=now - timedelta(minutes=1),
                expires_at=now + timedelta(hours=6),
            ),
            experiment_id="EXP-PAPER-SIM-1",
            execution_mode=ExecutionMode.PAPER,
            macro=MacroAssessment(
                regime="RISK_ON",
                directional_bias=MacroBias.BULLISH,
                confidence=Decimal("0.8"),
                fresh_until=now + timedelta(hours=1),
                model_version="sim",
            ),
            execute=True,
        )
        return (request,), snapshots

    def snapshots_for(
        self, obs: Observation, now: datetime
    ) -> dict[str, FeatureSnapshot]:
        snaps: dict[str, FeatureSnapshot] = {
            "NIFTY": underlying_snapshot(now=now, quality=obs.quality)
        }
        strikes = {LONG_SYMBOL: "24000", SHORT_SYMBOL: "24100"}
        for symbol, quote in obs.quotes.items():
            if obs.drop_monitor and symbol == LONG_SYMBOL:
                continue
            snaps[symbol] = option_snapshot(
                now=now,
                symbol=symbol,
                strike=strikes[symbol],
                spec=quote,
                dte=obs.dte,
                quality=obs.quality,
                extra_features=obs.extra_features,
            )
        return snaps

    def _has_nifty_exposure(self) -> bool:
        return any(
            position.contract.underlying == "NIFTY"
            for position in self.broker.get_positions()
        )

    def apply(self, obs: Observation) -> TraceRow:
        """Advance the clock, feed one observation, tick the real session."""
        self.clock.set(obs.at)
        self.broker.timeout_next_exit = obs.timeout_next_exit
        self.broker.partial_sell_qty = obs.partial_sell_qty
        self._pending_obs = obs
        snaps = self.snapshots_for(obs, obs.at)
        for symbol, snap in snaps.items():
            if symbol != "NIFTY":
                self.broker.publish_quote(symbol, snap.market)
        reviews_before = self._review_ids()
        orders_before = len(self.broker.list_orders())
        if self.clock.now_utc().astimezone(IST).time() >= time(15, 40):
            self.session.run(once=True)
        else:
            self.session.tick()
            self._persist()
        return self._trace_row(obs, reviews_before, orders_before)

    def apply_quote(self, obs: Observation) -> TraceRow:
        """Feed a protection-loop quote without running the entry poll."""
        self.clock.set(obs.at)
        snaps = self.snapshots_for(obs, obs.at)
        self.coordinator.seed_snapshots(snaps)
        quote_map = {
            symbol: snap.market for symbol, snap in snaps.items() if symbol != "NIFTY"
        }
        quote_result = self.coordinator.publish_quotes(
            quote_map,
            received_at=obs.at,
        )
        reviews_before = self._review_ids()
        orders_before = len(self.broker.list_orders())
        self._persist()
        row = self._trace_row(obs, reviews_before, orders_before)
        if quote_result is not None and quote_result.detection_latency_ms is not None:
            row.extras["stop_detection_latency_ms"] = quote_result.detection_latency_ms
        return row

    def _review_ids(self) -> frozenset[str]:
        ids: set[str] = set()
        for row in self.store.list_position_lifecycle():
            for review in row.reviews:
                ids.add(review.review_id)
        return frozenset(ids)

    def _trace_row(
        self, obs: Observation, reviews_before: frozenset[str], orders_before: int
    ) -> TraceRow:
        positions = self.runner.trade_manager.list_positions()
        broker_pos = self.broker.get_positions()
        lifecycle = self.store.list_position_lifecycle()
        new_reviews: list[str] = []
        frozen = "none"
        for row in lifecycle:
            frozen = (
                f"policy={row.position.exit_policy.policy_id} "
                f"stop={row.position.exit_policy.stop_price} "
                f"target={row.position.exit_policy.target_price} "
                f"scope={row.position.exit_policy.scope.value} "
                f"time_exit={row.position.exit_policy.time_exit}"
            )
            for review in row.reviews:
                if review.review_id in reviews_before:
                    continue
                new_reviews.append(
                    f"{review.slot_id.value}/{review.action.value}/"
                    f"{review.reason_code.value} submitted={review.submitted} "
                    f"({review.detail})"
                )
        position_txt = "FLAT"
        if positions:
            pos = positions[-1]
            legs = ",".join(
                f"{leg.side.value} {leg.quantity_contracts} {leg.contract.symbol}"
                f"@{leg.average_entry_price}"
                for leg in pos.legs
            )
            position_txt = (
                f"{pos.trade_id} {pos.state.value} legs=[{legs}] "
                f"protect={pos.protective_order_ids} {frozen}"
            )
        elif broker_pos:
            position_txt = "manager-empty broker=" + ",".join(
                f"{item.side.value} {item.quantity_contracts} {item.contract.symbol}"
                for item in broker_pos
            )
        new_orders = self.broker.list_orders()[orders_before:]
        order_txt = "none"
        fill_txt = "none"
        if new_orders:
            order_txt = " | ".join(
                f"{event.state.value} {event.command.side.value} "
                f"{event.command.quantity_contracts} {event.command.contract.symbol} "
                f"limit={event.command.limit_price} idemp={event.identity.idempotency_key[:8]}"
                for event in new_orders
            )
            fills = [
                event
                for event in new_orders
                if event.average_fill_price is not None and event.filled_quantity
            ]
            if fills:
                fill_txt = " | ".join(
                    f"{event.command.side.value} {event.filled_quantity} "
                    f"@ {event.average_fill_price} ({event.state.value})"
                    for event in fills
                )
        risk_txt = "none"
        for row in lifecycle:
            decision = row.risk_decision
            risk_txt = (
                f"{decision.action.value} reservation={decision.capital_reservation_id} "
                f"codes={tuple(code.value for code in decision.reason_codes)}"
            )
        reservations = [
            f"{item.reservation_id}:{item.state.value}:{item.amount}"
            for item in self.store.list_reservations()
        ]
        pnl = self._pnl_summary()
        review_txt = " | ".join(new_reviews) if new_reviews else "no-review-row"
        exit_eval = self._last_exit_hint(obs)
        if exit_eval:
            review_txt = f"{review_txt}; continuous={exit_eval}"
        market = ",".join(
            f"{sym} {q.bid}/{q.ask} last={q.last or q.bid} dte={obs.dte} {obs.quality.value}"
            for sym, q in obs.quotes.items()
        )
        if obs.note:
            market = f"{obs.note}; {market}"
        return TraceRow(
            timestamp=obs.at.astimezone(IST).isoformat(),
            market_observation=market,
            position_state=position_txt,
            review_exit_decision=review_txt,
            risk_decision=risk_txt,
            order_state=order_txt,
            fill=fill_txt,
            final_position_pnl=pnl,
            extras={
                "broker_quotes": sorted(self.broker._quotes),
                "reservations": reservations,
                "review_slots": [
                    (slot.value, day.isoformat())
                    for slot, day in self.store.list_review_slot_runs(
                        obs.at.astimezone(IST).date()
                    )
                ],
                "alerts": [
                    f"{alert.reason_code.value}:{alert.detail}"
                    for alert in self.runner.last_recovery.alerts
                ],
                "entries_blocked": self.runner.last_recovery.entries_blocked,
                "open_broker": [
                    f"{item.side.value}:{item.quantity_contracts}:{item.contract.symbol}"
                    for item in self.broker.get_positions()
                ],
            },
        )

    def _last_exit_hint(self, obs: Observation) -> str:
        positions = [
            item
            for item in self.runner.trade_manager.list_positions()
            if item.state is not TradeState.CLOSED
        ]
        if not positions:
            closed = [
                item
                for item in self.runner.trade_manager.list_positions()
                if item.state is TradeState.CLOSED
            ]
            if closed:
                return f"CLOSED via {closed[-1].state.value}"
            return ""
        return f"{positions[-1].state.value}"

    def _pnl_summary(self) -> str:
        events = [
            event
            for event in self.broker.list_orders()
            if event.average_fill_price is not None and event.filled_quantity
        ]
        if not events:
            return "no-fills"
        cash = Decimal("0")
        lot_size = Decimal("75")
        itemized: list[str] = []
        for event in events:
            px = event.average_fill_price
            assert px is not None
            qty = Decimal(event.filled_quantity)
            signed = qty if event.command.side is Side.SELL else -qty
            cash += signed * px.value
            intended = event.command.limit_price
            slip = Decimal("0")
            if intended is not None:
                if event.command.side is Side.BUY:
                    slip = max(px.value - intended.value, Decimal("0"))
                else:
                    slip = max(intended.value - px.value, Decimal("0"))
            itemized.append(
                f"{event.command.side.value} {event.filled_quantity}@{px} "
                f"limit={intended} slip={slip}"
            )
        charges: Money | None = None
        confirmed = self.fill_model.charges_per_lot.is_verified
        if confirmed:
            per = self.fill_model.charges_per_lot.require("fill_model.charges_per_lot")
            round_trips = (
                Decimal(
                    sum(
                        e.filled_quantity for e in events if e.command.side is Side.SELL
                    )
                )
                / lot_size
            )
            charges = Money.of(str(per * round_trips), Currency.INR)
        net = cash - (charges.amount if charges is not None else Decimal("0"))
        return (
            f"gross={cash} charges={charges} confirmed={confirmed} net={net} "
            f"fills=[{'; '.join(itemized)}]"
        )


def restart_world(world: SimWorld, *, at: datetime) -> SimWorld:
    """Flush to disk, spawn a new Python process, then reopen the world."""
    world.close()
    job = {
        "store_path": str(world.store_path),
        "broker_path": str(world.broker_path),
        "clock": at.isoformat(),
    }
    job_path = world.workdir / "restart_job.json"
    job_path.write_text(json.dumps(job), encoding="utf-8")
    result = world.workdir / "restart_result.json"
    if result.exists():
        result.unlink()
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + str(ROOT)
    completed = subprocess.run(  # noqa: S603
        [sys.executable, str(WORKER), str(job_path), str(result)],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(ROOT),
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"restart worker failed rc={completed.returncode} "
            f"stderr={completed.stderr[-2000:]}"
        )
    meta = json.loads(result.read_text(encoding="utf-8"))
    if int(meta["pid"]) == os.getpid():
        raise RuntimeError("restart worker reused the parent pid")
    clock = FrozenClock(at)
    reopened = SimWorld(world.workdir, clock=clock)
    reopened.restart_meta = meta
    return reopened


def format_trace(rows: list[TraceRow]) -> str:
    lines: list[str] = []
    for row in rows:
        lines.append(
            f"{row.timestamp} → {row.market_observation} → {row.position_state} → "
            f"{row.review_exit_decision} → {row.risk_decision} → {row.order_state} → "
            f"{row.fill} → {row.final_position_pnl}"
        )
    return "\n".join(lines)


__all__ = [
    "DEPTH",
    "ENTRY_UTC",
    "EOD_UTC",
    "IST",
    "LONG_SYMBOL",
    "NEXT_OPEN",
    "ROOT",
    "SHORT_SYMBOL",
    "SLOT_1030",
    "SLOT_1100",
    "SLOT_1110",
    "SLOT_1430",
    "Observation",
    "QuoteSpec",
    "SimWorld",
    "TraceRow",
    "format_trace",
    "restart_world",
]
