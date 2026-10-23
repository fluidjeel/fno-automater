#!/usr/bin/env -S uv run python
"""Flatten non-M2 opens and purge M1/M3/M4 paper history (Oracle M2 branch)."""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from trading.domain.contracts.lifecycle import PositionLifecycleRecord
from trading.domain.enums import ModeId, TradeState
from trading.domain.enums import TradeState
from trading.storage.trading_store import TradingEventType

M2 = ModeId.M2_DIRECTIONAL.value
NON_M2 = {
    ModeId.M1_CAS.value,
    ModeId.M3_TACTICAL_POSITIONAL.value,
    ModeId.M4_STRATEGIC_POSITIONAL.value,
}


def _mode_of(payload: str) -> str | None:
    rec = PositionLifecycleRecord.model_validate_json(payload)
    mode = rec.mode_id or (rec.position.mode_id if rec.position else None)
    return mode.value if mode is not None else None


def _admin_close_opens(store_path: Path, *, dry_run: bool) -> int:
    """Mark every non-CLOSED lifecycle CLOSED so broker purge can proceed."""
    conn = sqlite3.connect(store_path)
    closed = 0
    try:
        rows = conn.execute(
            "select trade_id, state, payload from position_lifecycle"
        ).fetchall()
        for trade_id, state, payload in rows:
            if state == TradeState.CLOSED.value:
                continue
            if dry_run:
                closed += 1
                continue
            rec = PositionLifecycleRecord.model_validate_json(payload)
            now = datetime.now(tz=UTC)
            position = rec.position.model_copy(
                update={
                    "state": TradeState.CLOSED,
                    "closed_at": now,
                    "as_of": now,
                }
            )
            updated = rec.model_copy(
                update={"position": position, "exit_order_ids": (), "as_of": now}
            )
            conn.execute(
                "update position_lifecycle set state = ?, payload = ? where trade_id = ?",
                (TradeState.CLOSED.value, updated.model_dump_json(), trade_id),
            )
            closed += 1
        if not dry_run:
            conn.commit()
    finally:
        conn.close()
    return closed


def _reset_broker_flat(repo_root: Path, *, dry_run: bool) -> None:
    path = repo_root / "data/paper/broker_state.json"
    if not path.is_file() or dry_run:
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    data["positions"] = []
    data["orders"] = []
    if isinstance(data.get("funds"), dict):
        data["funds"]["margin_used"] = {"amount": "0", "currency": "INR"}
        data["funds"]["margin_available"] = data["funds"].get(
            "equity", {"amount": "700000", "currency": "INR"}
        )
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _purge_sqlite(db_path: Path, *, dry_run: bool) -> tuple[int, int]:
    conn = sqlite3.connect(db_path)
    try:
        lifecycles = conn.execute(
            "select trade_id, state, payload from position_lifecycle"
        ).fetchall()
        drop_trades: list[str] = []
        keep_trades: list[str] = []
        for trade_id, _state, payload in lifecycles:
            mode = _mode_of(payload)
            if mode == M2:
                keep_trades.append(trade_id)
            else:
                drop_trades.append(trade_id)
        if dry_run:
            return len(drop_trades), len(keep_trades)
        for tid in drop_trades:
            conn.execute("delete from position_lifecycle where trade_id = ?", (tid,))
        if drop_trades:
            placeholders = ",".join("?" for _ in drop_trades)
            conn.execute(
                f"delete from trading_events where json_extract(payload, '$.trade_id') in ({placeholders})",  # noqa: S608
                drop_trades,
            )
            conn.execute(
                f"delete from trading_events where json_extract(payload, '$.identity.trade_id') in ({placeholders})",  # noqa: S608
                drop_trades,
            )
        conn.commit()
        return len(drop_trades), len(keep_trades)
    finally:
        conn.close()


def _remove_m1_artifacts(repo_root: Path, *, dry_run: bool) -> list[str]:
    removed: list[str] = []
    for rel in (
        "data/paper/m1_episodes.json",
        "data/paper/m1_latency_report.json",
        "data/paper/cas_depth_collector_state.json",
    ):
        path = repo_root / rel
        if not path.exists():
            continue
        removed.append(rel)
        if not dry_run:
            path.unlink()
    return removed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(".").resolve(),
        help="fno-automated root (default: cwd)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report actions without mutating state",
    )
    parser.add_argument(
        "--skip-flatten",
        action="store_true",
        help="Only purge closed history; do not flatten open positions",
    )
    args = parser.parse_args()
    root = args.repo_root.resolve()
    store = root / "data/paper/trading.sqlite"
    if not store.is_file():
        raise SystemExit(f"missing paper store: {store}")

    stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = root / "data/paper/backups" / f"pre-m2-only-{stamp}"
    if not args.dry_run:
        backup.mkdir(parents=True, exist_ok=True)
        shutil.copy2(store, backup / "trading.sqlite")

    admin_closed = _admin_close_opens(store, dry_run=args.dry_run)
    _reset_broker_flat(root, dry_run=args.dry_run)
    if not args.dry_run:
        conn = sqlite3.connect(store)
        try:
            conn.execute("delete from entry_freeze")
            conn.execute("delete from mode_entry_freeze")
            conn.execute("delete from protection_state")
            conn.execute("delete from session_protection")
            conn.execute("delete from reservations")
            conn.commit()
        finally:
            conn.close()

    dropped, kept = _purge_sqlite(store, dry_run=args.dry_run)
    artifacts = _remove_m1_artifacts(root, dry_run=args.dry_run)
    print(
        json.dumps(
            {
                "admin_closed_opens": admin_closed,
                "purged_non_m2_lifecycles": dropped,
                "kept_m2_lifecycles": kept,
                "removed_artifacts": artifacts,
                "backup_dir": str(backup) if not args.dry_run else None,
                "dry_run": args.dry_run,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
