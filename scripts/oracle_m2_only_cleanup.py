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
from trading.ops.paper_portfolio_reset import reset_paper_portfolio

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

    if not args.skip_flatten:
        result = reset_paper_portfolio(
            root,
            dry_run=args.dry_run,
            backup=False,
        )
        print(
            json.dumps(
                {
                    "flatten": result.detail,
                    "closed": list(result.closed_trade_ids),
                    "admin_closed": list(result.administratively_closed),
                },
                indent=2,
            )
        )

    dropped, kept = _purge_sqlite(store, dry_run=args.dry_run)
    artifacts = _remove_m1_artifacts(root, dry_run=args.dry_run)
    print(
        json.dumps(
            {
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
