"""PAPER-only portfolio reset: flatten opens, clear latches, fresh broker book."""

from __future__ import annotations

import json
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from trading.broker.ports import BrokerFunds
from trading.domain.contracts import PositionLifecycleRecord, ReconciliationEvent
from trading.domain.contracts.entry_freeze import EntryFreezeRecord
from trading.domain.contracts.snapshot import MarketQuote
from trading.domain.enums import (
    Environment,
    ModeId,
    ReconciliationTrigger,
    SystemState,
    TradeState,
    Trigger,
)
from trading.domain.primitives import Currency, Money
from trading.ops.paper_exit_recovery import (
    _load_quote_book,
    _RecoveryContext,
    _resolve_lifecycle_reconciliation,
    _retry_records,
    open_paper_recovery_context,
    synthetic_quotes_for_open_positions,
)
from trading.storage.trading_store import TradingEventType, TradingStore

__all__ = ["PaperPortfolioResetResult", "reset_paper_portfolio"]


@dataclass(frozen=True, slots=True)
class PaperPortfolioResetResult:
    closed_trade_ids: tuple[str, ...]
    administratively_closed: tuple[str, ...]
    resolved_reconciliation_ids: tuple[str, ...]
    entries_released: bool
    detail: str
    backup_dir: Path | None = None


def reset_paper_portfolio(
    repo_root: Path,
    *,
    quotes: Mapping[str, MarketQuote] | None = None,
    quotes_json: Path | None = None,
    dry_run: bool = False,
    backup: bool = True,
) -> PaperPortfolioResetResult:
    """Close all open PAPER trades and clear entry blocks for a fresh session."""
    ctx = open_paper_recovery_context(repo_root)
    if ctx.account.config.environment is not Environment.PAPER:
        ctx.store.close()
        raise ValueError("reset_paper_portfolio is PAPER-only")
    backup_dir: Path | None = None
    if backup and not dry_run:
        backup_dir = _backup_paper_state(ctx)
    open_records = _open_lifecycle_records(ctx.store)
    quote_book = _load_quote_book(quotes, quotes_json)
    if not quote_book:
        quote_book = synthetic_quotes_for_open_positions(ctx.store)
    now = ctx.runner._clock.now_utc()
    if dry_run:
        ctx.store.close()
        return PaperPortfolioResetResult(
            closed_trade_ids=(),
            administratively_closed=(),
            resolved_reconciliation_ids=(),
            entries_released=False,
            detail=f"dry-run: would flatten {len(open_records)} open trade(s)",
            backup_dir=backup_dir,
        )
    closed: list[str] = []
    admin_closed: list[str] = []
    ctx.runner.recover_lifecycle()
    if open_records:
        recovered, _, _ = _retry_records(
            ctx,
            open_records,
            quote_book,
            now=now,
            verbose=False,
            log=[],
        )
        closed.extend(recovered)
    for record in _open_lifecycle_records(ctx.store):
        if record.trade_id in closed:
            continue
        _administrative_close(ctx, record, now=now)
        admin_closed.append(record.trade_id)
    _purge_broker_exposure(ctx)
    _clear_reservations(ctx.store)
    _clear_entry_freezes(ctx, now=now)
    _reset_m1_episode_ledger(ctx)
    resolved_trade = tuple(dict.fromkeys((*closed, *admin_closed)))
    resolved_events = _resolve_lifecycle_reconciliation(
        ctx.store,
        trade_ids=resolved_trade,
        now=now,
        id_factory=ctx.id_factory,
    ) + _resolve_all_reconciliation(ctx, now=now)
    ctx.runner._lifecycle_recovered = False
    ctx.runner.recover_lifecycle()
    ctx.store.set_system_state(SystemState.READY, last_reconciliation_ref=None)
    boot = ctx.runner._services.reconciler.boot_reconcile(ctx.account.config.account_id)
    ctx.runner._maybe_release_entry_freeze(reconcile_blocked=boot.result.entries_blocked)
    controls = ctx.runner._services.controls
    if controls.state.entry_frozen:
        controls.release_entry_freeze(
            actor="ops-reset",
            scope="paper/portfolio-reset",
            trigger=Trigger.RECONCILIATION,
            incident_id=ctx.id_factory.new_id("INC"),
        )
    if controls.state.protection_degraded:
        controls.restore_protection(
            actor="ops-reset",
            scope="paper/portfolio-reset",
            trigger=Trigger.RECONCILIATION,
            incident_id=ctx.id_factory.new_id("INC"),
        )
    ctx.broker.persist_state()
    entries_released = not boot.result.entries_blocked
    ctx.store.close()
    return PaperPortfolioResetResult(
        closed_trade_ids=tuple(closed),
        administratively_closed=tuple(admin_closed),
        resolved_reconciliation_ids=resolved_events,
        entries_released=entries_released,
        detail=(
            f"flattened {len(closed)} trade(s); administratively closed "
            f"{len(admin_closed)}; resolved {len(resolved_events)} reconciliation "
            f"event(s)"
        ),
        backup_dir=backup_dir,
    )


def _open_lifecycle_records(store: TradingStore) -> tuple[PositionLifecycleRecord, ...]:
    return tuple(
        record
        for record in store.list_position_lifecycle()
        if record.position.state is not TradeState.CLOSED
    )


def _administrative_close(
    ctx: _RecoveryContext,
    record: PositionLifecycleRecord,
    *,
    now: datetime,
) -> None:
    """Mark lifecycle CLOSED and drop broker legs when exit retry did not finish."""
    position = record.position.model_copy(
        update={
            "state": TradeState.CLOSED,
            "closed_at": now,
            "as_of": now,
        }
    )
    closed = record.model_copy(
        update={
            "position": position,
            "exit_order_ids": (),
            "as_of": now,
        }
    )
    ctx.store.upsert_position_lifecycle(
        closed,
        event_id=ctx.id_factory.new_id("PLC"),
    )
    payload = ctx.broker.dump_state()
    positions = payload.get("positions")
    if isinstance(positions, list):
        payload["positions"] = [
            row
            for row in positions
            if not (
                isinstance(row, dict) and row.get("trade_id") == record.trade_id
            )
        ]
        ctx.broker.load_state(payload)


def _purge_broker_exposure(ctx: _RecoveryContext) -> None:
    """Reset paper broker to flat ₹7L equity with no working orders."""
    currency = Currency.INR
    equity = Money.of("700000", currency)
    funds = BrokerFunds(
        account_id=ctx.account.config.account_id,
        as_of=ctx.runner._clock.now_utc(),
        equity=equity,
        margin_used=Money.zero(currency),
        margin_available=equity,
    )
    payload = ctx.broker.dump_state()
    payload["funds"] = funds.model_dump(mode="json")
    payload["positions"] = []
    payload["orders"] = []
    ctx.broker.load_state(payload)


def _clear_reservations(store: TradingStore) -> None:
    store._conn.execute("DELETE FROM reservations")
    store._conn.commit()


def _clear_entry_freezes(ctx: _RecoveryContext, *, now: datetime) -> None:
    cleared = EntryFreezeRecord(
        entries_blocked=False,
        reason_code=None,
        detail=None,
        updated_at=now,
    )
    ctx.store.upsert_entry_freeze(cleared, event_id=ctx.id_factory.new_id("FRZ"))
    for mode_id in ModeId:
        ctx.store.upsert_entry_freeze(
            cleared.model_copy(update={"mode_id": mode_id}),
            event_id=ctx.id_factory.new_id("FRZ"),
            mode_id=mode_id,
        )


def _resolve_all_reconciliation(
    ctx: _RecoveryContext, *, now: datetime
) -> tuple[str, ...]:
    resolved: list[str] = []
    for stored in ctx.store.read_events():
        if stored.event_type is not TradingEventType.RECONCILIATION_EVENT:
            continue
        original = stored.deserialize()
        if not isinstance(original, ReconciliationEvent):
            continue
        if original.is_resolved:
            continue
        repair = ReconciliationEvent(
            event_id=ctx.id_factory.new_id("REC"),
            scope=original.scope,
            trigger=ReconciliationTrigger.MANUAL,
            expected_local_ref=original.expected_local_ref,
            observed_broker_ref=original.observed_broker_ref,
            difference_class=original.difference_class,
            severity=original.severity,
            reason_code=original.reason_code,
            repair_action=(
                f"{original.repair_action or 'operator repair'}; "
                "resolved by trading ops reset-paper-portfolio"
            ),
            repair_succeeded=True,
            entries_blocked=False,
            detected_at=now,
            resolved_at=now,
        )
        ctx.store.append(
            TradingEventType.RECONCILIATION_EVENT,
            repair,
            event_id=repair.event_id,
        )
        resolved.append(repair.event_id)
    return tuple(resolved)


def _reset_m1_episode_ledger(ctx: _RecoveryContext) -> None:
    path = ctx.repo_root / "data" / "paper" / "m1_episodes.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")


def _backup_paper_state(ctx: _RecoveryContext) -> Path:
    stamp = ctx.runner._clock.now_utc().strftime("%Y%m%dT%H%M%SZ")
    backup_dir = ctx.repo_root / "data" / "paper" / "backups" / f"pre-reset-{stamp}"
    backup_dir.mkdir(parents=True, exist_ok=True)
    store_path = ctx.repo_root / ctx.session_cfg.store_path
    broker_path = ctx.state_path
    if store_path.is_file():
        shutil.copy2(store_path, backup_dir / store_path.name)
    if broker_path.is_file():
        shutil.copy2(broker_path, backup_dir / broker_path.name)
    meta = {
        "store": str(store_path),
        "broker_state": str(broker_path),
        "open_trades": [r.trade_id for r in _open_lifecycle_records(ctx.store)],
    }
    (backup_dir / "meta.json").write_text(
        json.dumps(meta, indent=2),
        encoding="utf-8",
    )
    return backup_dir
