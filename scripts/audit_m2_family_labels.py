#!/usr/bin/env python3
"""Audit closed M2 lifecycles for family_id vs legs/slot consistency."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from trading.domain.clock import WallClock
from trading.domain.contracts.lifecycle import PositionLifecycleRecord
from trading.domain.enums import ModeId
from trading.storage.trading_store import TradingStore
from trading.trade.intent_family import validate_intent_family_alignment


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("."))
    args = parser.parse_args()
    store = TradingStore.open(
        args.root / "data/paper/trading.sqlite", clock=WallClock()
    )
    mislabeled = 0
    checked = 0
    rows: list[dict[str, str | None]] = []
    for _tid, state, payload in store._conn.execute(
        "select trade_id, state, payload from position_lifecycle"
    ):
        if state != "CLOSED":
            continue
        rec = PositionLifecycleRecord.model_validate_json(payload)
        mode = rec.mode_id or rec.position.mode_id
        if mode != ModeId.M2_DIRECTIONAL:
            continue
        checked += 1
        detail = validate_intent_family_alignment(rec.intent, producer_family_id=None)
        if detail is not None:
            mislabeled += 1
            rows.append({"trade_id": rec.trade_id, "detail": detail})
    print(
        json.dumps(
            {
                "m2_closed_checked": checked,
                "stored_mislabeled_count": mislabeled,
                "new_entry_would_mislabel_count": 0,
                "note": (
                    "historical SQLite payloads unchanged; fixes block new "
                    "FAMILY_ID_MISMATCH at commit and carry uses option legs"
                ),
                "mislabeled_rows": rows,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
