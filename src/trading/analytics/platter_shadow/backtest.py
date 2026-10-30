"""Shadow logging and rubric-only backtest."""

from __future__ import annotations

import csv
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from trading.ai.platter_agent.rubric import score_platter
from trading.data.platter_pipeline.config import load_platter_pipeline_config
from trading.trade.platter_gate import map_platter_gate

__all__ = ["run_rubric_backtest", "write_shadow_record"]


def write_shadow_record(repo_root: Path, record: dict[str, Any]) -> None:
    """Append one shadow comparison line."""
    config = load_platter_pipeline_config(repo_root / "config" / "platter_pipeline.yaml")
    path = repo_root / config.shadow_mode.log_path
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")


def run_rubric_backtest(repo_root: Path, *, output_csv: Path) -> int:
    """Replay platters with rubric-only agent (no LLM)."""
    config = load_platter_pipeline_config(repo_root / "config" / "platter_pipeline.yaml")
    root = repo_root / "data" / "platter" / "platters"
    rows: list[dict[str, str]] = []
    if not root.is_dir():
        output_csv.write_text("", encoding="utf-8")
        return 0
    for path in sorted(root.rglob("platter_*.json")):
        platter = json.loads(path.read_text(encoding="utf-8"))
        rubric = score_platter(platter, config)
        gate = map_platter_gate(platter, rubric.direction, config=config)
        rows.append(
            {
                "platter_id": str(platter.get("platter_id")),
                "platter_mark_ist": str(platter.get("platter_mark_ist")),
                "direction": rubric.direction,
                "action": gate.action.value,
                "score_total": str(rubric.score_total),
                "ltp": str(platter.get("spot", {}).get("ltp") or "0"),
            }
        )
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with output_csv.open("w", encoding="utf-8", newline="") as handle:
        if not rows:
            return 0
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)
