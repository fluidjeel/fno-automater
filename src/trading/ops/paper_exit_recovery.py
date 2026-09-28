"""PAPER-only operator recovery for stuck exit legs and lifecycle reconciliation."""

from __future__ import annotations

import json
from collections.abc import Mapping
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
    OrderEvent,
    PositionLifecycleRecord,
    ReconciliationEvent,
)
from trading.domain.contracts.common import DataQualityReport, Lineage, Versions
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
    TradeState,
)
from trading.domain.ids import SequentialIdFactory
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


@dataclass(frozen=True, slots=True)
class PaperExitRecoveryResult:
    trade_ids: tuple[str, ...]
    resolved_event_ids: tuple[str, ...]
    entries_released: bool
    detail: str


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
) -> PaperExitRecoveryResult:
    """Retry/force-exit stuck PAPER legs at current quotes. Never usable in LIVE."""
    quote_book = _load_quote_book(quotes, quotes_json)
    wall = clock or WallClock()
    ctx = _open_recovery_context(repo_root, clock=wall)
    stuck = _stuck_lifecycle_records(ctx.store, trade_id=trade_id)
    if dry_run:
        ctx.store.close()
        return PaperExitRecoveryResult(
            trade_ids=tuple(item.trade_id for item in stuck),
            resolved_event_ids=(),
            entries_released=False,
            detail=f"dry-run: {len(stuck)} trade(s) would be retried",
        )
    recovered = _retry_records(ctx, stuck, quote_book, now=wall.now_utc())
    resolved_ids = _resolve_lifecycle_reconciliation(
        ctx.store,
        trade_ids=tuple(item.trade_id for item in stuck),
        now=wall.now_utc(),
        id_factory=ctx.id_factory,
    )
    boot = ctx.runner._services.reconciler.boot_reconcile(ctx.account.config.account_id)
    ctx.runner._maybe_release_entry_freeze(
        reconcile_blocked=boot.result.entries_blocked
    )
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
    )


def _load_quote_book(
    quotes: Mapping[str, MarketQuote] | None,
    quotes_json: Path | None,
) -> dict[str, MarketQuote]:
    quote_book = dict(quotes or {})
    if quotes_json is not None:
        quote_book.update(load_quotes_from_json(quotes_json))
    if not quote_book:
        raise ValueError("provide quotes or --quotes-json with current leg prices")
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
) -> list[str]:
    recovered: list[str] = []
    for record in stuck:
        snapshots = _snapshots_for_record(record, quote_book, now=now)
        for symbol, quote in quote_book.items():
            if symbol in snapshots:
                ctx.broker.publish_quote(symbol, quote)
        position = ctx.runner.trade_manager.get_position(record.trade_id)
        if position is None:
            continue
        if position.state is TradeState.EXIT_PENDING:
            ctx.runner.trade_manager.apply_exit_evaluation(
                record.trade_id,
                ExitEvaluation(
                    kind=ExitKind.STOP,
                    reason_code=ReasonCode.OK,
                    detail="operator retry-stuck-paper-exits",
                    updated_policy=position.exit_policy,
                ),
            )
        pending = ctx.runner.trade_manager.get_position(record.trade_id)
        if pending is None:
            continue
        intent, decision = ctx.runner.open_book[record.trade_id]
        ctx.runner._submit_exit(intent, decision, pending, snapshots)
        after = ctx.runner.trade_manager.get_position(record.trade_id)
        if after is not None and after.state is TradeState.CLOSED:
            recovered.append(record.trade_id)
    return recovered


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
        if record.exit_order_ids and _exit_orders_terminal_rejected(store, record):
            stuck.append(record)
            continue
        scope = f"trade/{record.trade_id}/lifecycle"
        if _has_unresolved_lifecycle_event(store, scope):
            stuck.append(record)
    return tuple({item.trade_id: item for item in stuck}.values())


def _exit_orders_terminal_rejected(
    store: TradingStore, record: PositionLifecycleRecord
) -> bool:
    terminal = {OrderState.REJECTED, OrderState.CANCELLED, OrderState.EXPIRED}
    saw_terminal = False
    for order_id in record.exit_order_ids:
        event = _latest_order_event(store, order_id)
        if event is None:
            return False
        if event.state not in terminal:
            return False
        saw_terminal = True
    return saw_terminal


def _latest_order_event(store: TradingStore, order_id: str) -> OrderEvent | None:
    latest: OrderEvent | None = None
    for stored in store.read_events():
        if stored.event_type is not TradingEventType.ORDER_EVENT:
            continue
        event = stored.deserialize()
        if not isinstance(event, OrderEvent):
            continue
        if event.identity.internal_order_id != order_id:
            continue
        if latest is None or event.received_at >= latest.received_at:
            latest = event
    return latest


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
) -> dict[str, FeatureSnapshot]:
    snapshots: dict[str, FeatureSnapshot] = {}
    for leg in record.position.legs:
        quote = quotes.get(leg.contract.symbol)
        if quote is None:
            continue
        snapshots[leg.contract.symbol] = _recovery_snapshot(
            leg.contract, quote, now=now
        )
    return snapshots


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
