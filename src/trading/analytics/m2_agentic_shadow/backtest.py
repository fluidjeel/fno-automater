"""Shadow logging and rubric-only backtest."""

from __future__ import annotations

import csv
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from trading.ai.m2_agentic_agent.rubric import score_m2_agentic_decision
from trading.data.m2_agentic_pipeline.config import load_m2_agentic_pipeline_config
from trading.trade.m2_agentic_gate import map_m2_agentic_gate

__all__ = ["run_rubric_backtest", "write_shadow_record"]


def write_shadow_record(repo_root: Path, record: dict[str, Any]) -> None:
    """Append one shadow comparison line."""
    config = load_m2_agentic_pipeline_config(repo_root / "config" / "m2_agentic_pipeline.yaml")
    path = repo_root / config.shadow_mode.log_path
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")


def run_rubric_backtest(repo_root: Path, *, output_csv: Path) -> int:
    """Replay decision packets with rubric-only agent (no LLM)."""
    config = load_m2_agentic_pipeline_config(repo_root / "config" / "m2_agentic_pipeline.yaml")
    root = repo_root / "data" / "m2_agentic" / "decision_packets"
    rows: list[dict[str, str]] = []
    if not root.is_dir():
        output_csv.write_text("", encoding="utf-8")
        return 0
    for path in sorted(root.rglob("m2_agentic_*.json")):
        platter = json.loads(path.read_text(encoding="utf-8"))
        rubric = score_m2_agentic_decision(platter, config)
        gate = map_m2_agentic_gate(platter, rubric.direction, config=config)
        rows.append(
            {
                "m2_agentic_id": str(platter.get("m2_agentic_id")),
                "m2_agentic_mark_ist": str(platter.get("m2_agentic_mark_ist")),
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
