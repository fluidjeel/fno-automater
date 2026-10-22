#!/usr/bin/env python3
"""Summarize M2 A/B study JSONL results (Mode A live vs Mode B shadow)."""

from __future__ import annotations

import argparse
import json
from decimal import Decimal
from pathlib import Path


def _read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("."))
    args = parser.parse_args()
    base = args.root / "data/paper/m2_ab"
    a_rows = _read_jsonl(base / "results_mode_a.jsonl")
    b_rows = _read_jsonl(base / "results_mode_b.jsonl")
    cmp_rows = _read_jsonl(base / "comparison_events.jsonl")
    a_net = sum(Decimal(r.get("net_pnl_inr", "0")) for r in a_rows)
    b_net = sum(
        Decimal(r.get("net_pnl_inr", r.get("gross_pnl_inr", "0"))) for r in b_rows
    )
    early = [r for r in b_rows if r.get("exit_reason") == "EARLY_PRE_CLOSE"]
    divergent = [r for r in cmp_rows if r.get("event") == "divergent_close"]
    print(
        json.dumps(
            {
                "mode_a_trades": len(a_rows),
                "mode_a_total_net_inr": str(a_net),
                "mode_b_trades": len(b_rows),
                "mode_b_total_net_inr": str(b_net),
                "mode_b_early_exits": len(early),
                "divergent_closes": len(divergent),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
