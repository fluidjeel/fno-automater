"""PAPER-only operator recovery for stuck exit legs and lifecycle reconciliation."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from trading.broker.paper import PaperBroker
from trading.broker.ports import BrokerFunds
from trading.config import load_config, load_evaluation_config, load_risk_policy
from trading.config.discovery import DiscoveryConfig, load_discovery_config
from trading.config.evaluation import discovery_fill_models
from trading.config.loader import LoadedConfig
from trading.config.paper_data import load_paper_data_requirements
from trading.config.risk_policy import LoadedRiskPolicy
from trading.config.schema import Environment
from trading.domain.clock import Clock, WallClock
from trading.domain.contracts import (
    ContractRef,
    FeatureSnapshot,
    PositionLifecycleRecord,
    PositionState,
    ReconciliationEvent,
)
from trading.domain.contracts.common import DataQualityReport, Lineage, Versions
from trading.domain.contracts.order import OrderEvent
from trading.domain.contracts.snapshot import (
    DerivativesContext,
    MarketQuote,
    SnapshotTimes,
)
from trading.domain.enums import (
    DataQuality,
    ExecutionMode,
    InstrumentKind,
    OrderState,
    ReasonCode,
    ReconciliationTrigger,
    Severity,
    Side,
    TradeState,
)
from trading.domain.ids import SequentialIdFactory, derive_idempotency_key
from trading.domain.primitives import Currency, Money, Price, TickSize
from trading.runtime.isolation import assert_paper_isolation
from trading.runtime.paper_runner import PaperRunner
from trading.runtime.paper_session import PaperSessionConfig, load_paper_session_config
from trading.storage.trading_store import TradingEventType, TradingStore
from trading.trade.exits import ExitEvaluation, ExitKind

__all__ = [
    "PaperExitRecoveryResult",
    "load_quotes_from_json",
    "retry_stuck_paper_exits",
]

_TERMINAL_EXIT_FAILURE = frozenset(
    {OrderState.REJECTED, OrderState.CANCELLED, OrderState.EXPIRED}
)


@dataclass(frozen=True, slots=True)
class PaperExitRecoveryResult:
    trade_ids: tuple[str, ...]
    resolved_event_ids: tuple[str, ...]
    entries_released: bool
    detail: str
    leg_skips: tuple[str, ...] = ()
    verbose_log: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _RecoveryContext:
    repo_root: Path
    session_cfg: PaperSessionConfig
    account: LoadedConfig
    risk: LoadedRiskPolicy
    discovery_cfg: DiscoveryConfig | None
    store: TradingStore
    broker: PaperBroker
    runner: PaperRunner
    state_path: Path
    id_factory: SequentialIdFactory


def load_quotes_from_json(path: Path) -> dict[str, MarketQuote]:
    """Load symbol->quote mapping from operator JSON: {symbol: {bid, ask}}."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must be a JSON object keyed by symbol")
    quotes: dict[str, MarketQuote] = {}
    for symbol, row in payload.items():
        if not isinstance(symbol, str) or not isinstance(row, dict):
            raise ValueError(f"{path}: each entry must be symbol -> quote object")
        bid = _price(row.get("bid"), symbol=symbol, field="bid")
        ask = _price(row.get("ask"), symbol=symbol, field="ask")
        last_raw = row.get("last", row.get("bid"))
        last = _price(last_raw, symbol=symbol, field="last") if last_raw else None
        bid_size = int(row.get("bid_size", 300))
        ask_size = int(row.get("ask_size", 300))
        quotes[symbol] = MarketQuote(
            bid=bid,
            ask=ask,
            last=last,
            bid_size=bid_size,
            ask_size=ask_size,
        )
    return quotes


def retry_stuck_paper_exits(
    repo_root: Path,
    *,
    quotes: Mapping[str, MarketQuote] | None = None,
    quotes_json: Path | None = None,
    trade_id: str | None = None,
    clock: Clock | None = None,
    dry_run: bool = False,
    verbose: bool = False,
) -> PaperExitRecoveryResult:
    """Retry/force-exit stuck PAPER legs at current quotes. Never usable in LIVE."""
    quote_book = _load_quote_book(quotes, quotes_json)
    wall = clock or WallClock()
    ctx = _open_recovery_context(repo_root, clock=wall)
    stuck = _stuck_lifecycle_records(ctx.store, trade_id=trade_id)
    if not quote_book and not stuck:
        ctx.store.close()
        raise ValueError("provide quotes or --quotes-json with current leg prices")
    log: list[str] = []
    leg_skips: list[str] = []
    if dry_run:
        if verbose:
            for record in stuck:
                log.append(f"dry-run: would retry {record.trade_id}")
        ctx.store.close()
        return PaperExitRecoveryResult(
            trade_ids=(),
            resolved_event_ids=(),
            entries_released=False,
            detail=f"dry-run: {len(stuck)} trade(s) would be retried",
            verbose_log=tuple(log),
        )
    recovered, retry_log, retry_skips = _retry_records(
        ctx,
        stuck,
        quote_book,
        now=wall.now_utc(),
        verbose=verbose,
        log=log,
    )
    log.extend(retry_log)
    leg_skips.extend(retry_skips)
    resolved_ids = _resolve_lifecycle_reconciliation(
        ctx.store,
        trade_ids=tuple(recovered),
        now=wall.now_utc(),
        id_factory=ctx.id_factory,
    )
    if verbose and resolved_ids:
        log.append(f"resolved reconciliation events: {','.join(resolved_ids)}")
    boot = ctx.runner._services.reconciler.boot_reconcile(ctx.account.config.account_id)
    ctx.runner._maybe_release_entry_freeze(
        reconcile_blocked=boot.result.entries_blocked
    )
    if verbose:
        log.append(f"entries_released={not boot.result.entries_blocked}")
    if ctx.state_path.parent.exists():
        ctx.state_path.write_text(
            json.dumps(ctx.broker.dump_state(), indent=2) + "\n",
            encoding="utf-8",
        )
    ctx.store.close()
    return PaperExitRecoveryResult(
        trade_ids=tuple(recovered),
        resolved_event_ids=resolved_ids,
        entries_released=not boot.result.entries_blocked,
        detail=(
            f"retried {len(stuck)} trade(s); closed {len(recovered)}; "
            f"resolved {len(resolved_ids)} reconciliation event(s)"
        ),
        leg_skips=tuple(leg_skips),
        verbose_log=tuple(log),
    )


def _load_quote_book(
    quotes: Mapping[str, MarketQuote] | None,
    quotes_json: Path | None,
) -> dict[str, MarketQuote]:
    quote_book = dict(quotes or {})
    if quotes_json is not None:
        quote_book.update(load_quotes_from_json(quotes_json))
    return quote_book


def _open_recovery_context(repo_root: Path, *, clock: Clock) -> _RecoveryContext:
    session_cfg = load_paper_session_config(repo_root / "config" / "paper_session.yaml")
    account = load_config(
        repo_root / "config" / "paper.yaml", expect_environment=Environment.PAPER
    )
    if account.config.environment is not Environment.PAPER:
        raise ValueError("retry_stuck_paper_exits is PAPER-only")
    evaluation = load_evaluation_config(repo_root / "config" / "evaluation.yaml")
    risk = load_risk_policy(repo_root / "config" / "risk.yaml")
    discovery_cfg = None
    discovery_path = repo_root / "config" / "discovery.yaml"
    if discovery_path.is_file():
        discovery_cfg = load_discovery_config(discovery_path).config
    paper_data = load_paper_data_requirements(repo_root / "config" / "paper_data.yaml")
    now = clock.now_utc()
    store = TradingStore.open(repo_root / session_cfg.store_path, clock=clock)
    ids = SequentialIdFactory(now)
    if discovery_cfg is not None:
        touch_fill, shadow_fill = discovery_fill_models(
            evaluation.config.fill_model,
            model=discovery_cfg.fills.model,
            shadow_model=discovery_cfg.fills.shadow_model,
        )
        fill_model = touch_fill
        shadow_fill_model = shadow_fill
    else:
        fill_model = evaluation.config.fill_model
        shadow_fill_model = None
    funds = BrokerFunds(
        account_id=account.config.account_id,
        as_of=now,
        equity=Money.of("700000", Currency.INR),
        margin_used=Money.zero(Currency.INR),
        margin_available=Money.of("700000", Currency.INR),
    )
    broker = PaperBroker.for_session(
        clock=clock,
        id_factory=ids,
        funds=funds,
        fill_model=fill_model,
        shadow_fill_model=shadow_fill_model,
        future_margin_fraction=risk.config.paper_future_margin_fraction,
    )
    assert_paper_isolation(account.config.environment, ExecutionMode.PAPER, broker)
    state_path = repo_root / session_cfg.broker_state_path
    if state_path.is_file():
        broker.load_state(json.loads(state_path.read_text(encoding="utf-8")))
    runner = PaperRunner(
        account_config=account,
        risk_policy=risk,
        store=store,
        broker=broker,
        clock=clock,
        id_factory=ids,
        fill_model=fill_model,
        shadow_fill_model=shadow_fill_model,
        execution_mode=ExecutionMode.PAPER,
        paper_data_requirements=paper_data,
        discovery_config=discovery_cfg,
    )
    runner.recover_lifecycle()
    return _RecoveryContext(
        repo_root=repo_root,
        session_cfg=session_cfg,
        account=account,
        risk=risk,
        discovery_cfg=discovery_cfg,
        store=store,
        broker=broker,
        runner=runner,
        state_path=state_path,
        id_factory=ids,
    )


def _retry_records(
    ctx: _RecoveryContext,
    stuck: tuple[PositionLifecycleRecord, ...],
    quote_book: Mapping[str, MarketQuote],
    *,
    now: datetime,
    verbose: bool,
    log: list[str],
) -> tuple[list[str], list[str], list[str]]:
    recovered: list[str] = []
    retry_log: list[str] = []
    leg_skips: list[str] = []
    emit: Callable[[str], None] = retry_log.append if verbose else (lambda _msg: None)
    for record in stuck:
        emit(f"{record.trade_id}: begin recovery (state={record.position.state.value})")
        snapshots, skipped = _snapshots_for_record(record, quote_book, now=now)
        for leg_id, symbol, reason in skipped:
            message = f"{record.trade_id}/{leg_id} ({symbol}): skipped — {reason}"
            leg_skips.append(message)
            emit(message)
        if not snapshots:
            emit(f"{record.trade_id}: no leg quotes available; skipping trade")
            continue
        _publish_recovery_quotes(ctx, snapshots, quote_book)
        pending = _prepare_exit_retry(ctx, record, emit=emit)
        if pending is None:
            emit(f"{record.trade_id}: position missing after purge; skipping")
            continue
        book = ctx.runner.open_book.get(record.trade_id)
        if book is None:
            emit(f"{record.trade_id}: open book missing intent/decision; skipping")
            continue
        intent, decision = book
        fresh_record = ctx.store.get_position_lifecycle(record.trade_id)
        if fresh_record is not None and fresh_record.exit_order_ids:
            emit(
                f"{record.trade_id}: clearing residual exit_order_ids="
                f"{','.join(fresh_record.exit_order_ids)}"
            )
            _purge_trade_exit_state(ctx, fresh_record, emit=emit)
        emit(
            f"{record.trade_id}: submitting exit for "
            f"{len(pending.legs)} leg(s) at operator quotes"
        )
        events = ctx.runner._submit_exit(
            intent,
            decision,
            pending,
            snapshots,
            allow_partial_structure=True,
        )
        if not events:
            emit(f"{record.trade_id}: exit submit returned no order events")
        else:
            states = ",".join(sorted({event.state.value for event in events}))
            emit(f"{record.trade_id}: exit submit states={states}")
        after = ctx.runner.trade_manager.get_position(record.trade_id)
        if after is not None and after.state is TradeState.CLOSED:
            recovered.append(record.trade_id)
            emit(f"{record.trade_id}: closed")
        elif after is not None:
            emit(f"{record.trade_id}: still {after.state.value} after exit submit")
    return recovered, retry_log, leg_skips


def _prepare_exit_retry(
    ctx: _RecoveryContext,
    record: PositionLifecycleRecord,
    *,
    emit: Callable[[str], None],
) -> PositionState | None:
    """Drop stale failed exit orders so resubmit reaches the paper broker."""
    position = ctx.runner.trade_manager.get_position(record.trade_id)
    if position is None:
        return None
    if record.exit_order_ids or _broker_has_trade_orders(ctx.broker, record.trade_id):
        emit(
            f"{record.trade_id}: purging stale exit orders "
            f"(lifecycle_ids={','.join(record.exit_order_ids) or 'none'})"
        )
        _purge_trade_exit_state(ctx, record, emit=emit)
    position = ctx.runner.trade_manager.get_position(record.trade_id)
    if position is None:
        return None
    if position.state is TradeState.OPEN:
        emit(f"{record.trade_id}: transitioning OPEN -> EXIT_PENDING")
        position = ctx.runner.trade_manager.apply_exit_evaluation(
            record.trade_id,
            ExitEvaluation(
                kind=ExitKind.STOP,
                reason_code=ReasonCode.OK,
                detail="operator retry-stuck-paper-exits",
                updated_policy=position.exit_policy,
            ),
        )
    return position


def _publish_recovery_quotes(
    ctx: _RecoveryContext,
    snapshots: Mapping[str, FeatureSnapshot],
    quote_book: Mapping[str, MarketQuote],
) -> None:
    symbols = set(snapshots) | set(quote_book)
    for symbol in symbols:
        quote = quote_book.get(symbol)
        if quote is None and symbol in snapshots:
            quote = snapshots[symbol].market
        if quote is not None:
            ctx.broker.publish_quote(symbol, quote)


def _broker_has_trade_orders(broker: PaperBroker, trade_id: str) -> bool:
    return any(event.identity.trade_id == trade_id for event in broker.list_orders())


def _exit_idempotency_keys_for_trade(
    ctx: _RecoveryContext, record: PositionLifecycleRecord
) -> frozenset[str]:
    keys: set[str] = set(_idempotency_keys_for_orders(ctx.store, record.exit_order_ids))
    intent = record.intent
    account_id = ctx.account.config.account_id
    for leg in record.position.legs:
        exit_side = Side.SELL if leg.side is Side.BUY else Side.BUY
        exit_leg_id = f"{leg.leg_id}-exit"
        keys.add(
            derive_idempotency_key(
                account_id=account_id,
                strategy_id=intent.strategy_id,
                strategy_version=intent.strategy_version,
                intent_id=intent.intent_id,
                leg_id=exit_leg_id,
                side=exit_side.value,
                quantity_contracts=leg.quantity_contracts,
            )
        )
    for event in ctx.broker.list_orders():
        if event.identity.trade_id == record.trade_id:
            keys.add(event.identity.idempotency_key)
    return frozenset(keys)


def _purge_trade_exit_state(
    ctx: _RecoveryContext,
    record: PositionLifecycleRecord,
    *,
    emit: Callable[[str], None],
) -> None:
    """Remove stale exit orders from OMS, broker and lifecycle before resubmit."""
    keys = _exit_idempotency_keys_for_trade(ctx, record)
    oms = ctx.runner._services.oms
    for key in keys:
        oms._latest_by_key.pop(key, None)
    payload = ctx.broker.dump_state()
    orders = payload.get("orders")
    if isinstance(orders, list):
        filtered = [
            row
            for row in orders
            if not (
                isinstance(row, dict)
                and row.get("identity", {}).get("trade_id") == record.trade_id
            )
        ]
        if len(filtered) != len(orders):
            emit(
                f"{record.trade_id}: removed "
                f"{len(orders) - len(filtered)} broker order(s)"
            )
            payload["orders"] = filtered
            ctx.broker.load_state(payload)
    ctx.runner._write_lifecycle(record.trade_id, exit_order_ids=())


def _idempotency_keys_for_orders(
    store: TradingStore, order_ids: tuple[str, ...]
) -> frozenset[str]:
    wanted = set(order_ids)
    keys: set[str] = set()
    for stored in store.read_events():
        if stored.event_type is not TradingEventType.ORDER_EVENT:
            continue
        event = stored.deserialize()
        if not isinstance(event, OrderEvent):
            continue
        if event.identity.internal_order_id in wanted:
            keys.add(event.identity.idempotency_key)
    return frozenset(keys)


def _stuck_lifecycle_records(
    store: TradingStore, *, trade_id: str | None
) -> tuple[PositionLifecycleRecord, ...]:
    stuck: list[PositionLifecycleRecord] = []
    for record in store.list_position_lifecycle():
        if trade_id is not None and record.trade_id != trade_id:
            continue
        if record.position.state is TradeState.CLOSED:
            continue
        if record.position.state is TradeState.EXIT_PENDING:
            stuck.append(record)
            continue
        if record.exit_order_ids:
            stuck.append(record)
            continue
        scope = f"trade/{record.trade_id}/lifecycle"
        if _has_unresolved_lifecycle_event(store, scope):
            stuck.append(record)
    return tuple({item.trade_id: item for item in stuck}.values())


def _has_unresolved_lifecycle_event(store: TradingStore, scope: str) -> bool:
    for stored in store.read_events():
        if stored.event_type is not TradingEventType.RECONCILIATION_EVENT:
            continue
        event = stored.deserialize()
        if not isinstance(event, ReconciliationEvent):
            continue
        if event.scope != scope:
            continue
        if event.severity is Severity.CRITICAL and not event.is_resolved:
            return True
    return False


def _resolve_lifecycle_reconciliation(
    store: TradingStore,
    *,
    trade_ids: tuple[str, ...],
    now: datetime,
    id_factory: SequentialIdFactory,
) -> tuple[str, ...]:
    scopes = {f"trade/{trade_id}/lifecycle" for trade_id in trade_ids}
    resolved: list[str] = []
    for stored in store.read_events():
        if stored.event_type is not TradingEventType.RECONCILIATION_EVENT:
            continue
        original = stored.deserialize()
        if not isinstance(original, ReconciliationEvent):
            continue
        if original.scope not in scopes or original.is_resolved:
            continue
        repair = ReconciliationEvent(
            event_id=id_factory.new_id("REC"),
            scope=original.scope,
            trigger=ReconciliationTrigger.MANUAL,
            expected_local_ref=original.expected_local_ref,
            observed_broker_ref=original.observed_broker_ref,
            difference_class=original.difference_class,
            severity=original.severity,
            reason_code=original.reason_code,
            repair_action=(
                f"{original.repair_action or 'operator repair'}; "
                "resolved by trading ops retry-stuck-paper-exits"
            ),
            repair_succeeded=True,
            entries_blocked=False,
            detected_at=now,
            resolved_at=now,
        )
        store.append(
            TradingEventType.RECONCILIATION_EVENT,
            repair,
            event_id=repair.event_id,
        )
        resolved.append(repair.event_id)
    return tuple(resolved)


def _snapshots_for_record(
    record: PositionLifecycleRecord,
    quotes: Mapping[str, MarketQuote],
    *,
    now: datetime,
) -> tuple[dict[str, FeatureSnapshot], tuple[tuple[str, str, str], ...]]:
    snapshots: dict[str, FeatureSnapshot] = {}
    skipped: list[tuple[str, str, str]] = []
    for leg in record.position.legs:
        quote = quotes.get(leg.contract.symbol)
        if quote is None:
            skipped.append(
                (leg.leg_id, leg.contract.symbol, "no quote in operator quote book")
            )
            continue
        snapshots[leg.contract.symbol] = _recovery_snapshot(
            leg.contract, quote, now=now
        )
    return snapshots, tuple(skipped)


def _recovery_snapshot(
    contract: ContractRef, quote: MarketQuote, *, now: datetime
) -> FeatureSnapshot:
    derivatives = None
    if contract.instrument_kind in {InstrumentKind.OPTION, InstrumentKind.FUTURE}:
        derivatives = DerivativesContext(
            days_to_expiry=1,
            open_interest=0,
            option_type=contract.option_type,
            underlying_price=Price(
                value=Decimal("1"),
                tick=TickSize(value=Decimal("0.05")),
            ),
        )
    return FeatureSnapshot(
        snapshot_id=f"ops-{contract.symbol}",
        contract=contract,
        times=SnapshotTimes(
            event_time=now,
            source_time=now,
            receive_time=now,
            calculation_time=now,
        ),
        market=quote,
        derivatives=derivatives,
        feature_set_version="ops-exit-recovery",
        features={},
        quality=DataQualityReport(
            state=DataQuality.VALID,
            age_ms=0,
            warmup_complete=True,
            source_status="operator",
            reason_codes=(ReasonCode.OK,),
        ),
        lineage=Lineage(
            provider="operator",
            normalization_version="ops",
            versions=Versions(
                code_version="ops",
                config_version="ops",
                config_checksum="ops",
            ),
        ),
    )


def _price(raw: object, *, symbol: str, field: str) -> Price:
    if raw is None:
        raise ValueError(f"{symbol}: missing {field}")
    return Price(value=Decimal(str(raw)), tick=TickSize(value=Decimal("0.05")))
