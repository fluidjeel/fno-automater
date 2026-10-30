"""Append agent decision records and run retention."""

from __future__ import annotations

import json
import shutil
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from trading.data.platter_pipeline.config import PlatterPipelineConfig, load_platter_pipeline_config
from trading.trade.platter_gate import GateMapping

_IST = ZoneInfo("Asia/Kolkata")

__all__ = [
    "append_decision_record",
    "decisions_log_path",
    "run_retention_job",
    "run_weekly_rollup",
]


def decisions_log_path(repo_root: Path, session_date: str) -> Path:
    return (
        repo_root
        / "data"
        / "telemetry"
        / "agent_decisions"
        / session_date
        / "decisions.jsonl"
    )


def append_decision_record(
    repo_root: Path,
    *,
    platter: dict[str, Any],
    platter_snapshot_path: str,
    agent_output: dict[str, Any],
    gate: GateMapping,
    decision_id: str | None = None,
) -> str:
    """Append one AgentDecisionRecord line; return decision_id."""
    did = decision_id or str(uuid.uuid4())
    mark = datetime.fromisoformat(str(platter["platter_mark_ist"]))
    session_date = str(platter["session_date"])
    record = {
        "schema_version": "1",
        "decision_id": did,
        "session_date": session_date,
        "platter_mark_ist": platter["platter_mark_ist"],
        "platter_id": platter["platter_id"],
        "as_of_utc": platter.get("as_of_utc"),
        "environment": "PAPER",
        "platter_snapshot_path": platter_snapshot_path,
        "agent_output": agent_output,
        "gate_output": {
            "action": gate.action.value,
            "size_fraction": str(gate.size_fraction),
            "paper_mode": True,
        },
        "hard_rails": {
            "stale_data_veto": gate.stale_data_veto,
            "event_blackout_veto": gate.event_blackout_veto,
            "missing_platter": False,
        },
        "alignment": gate.alignment,
        "outcome": {},
    }
    path = decisions_log_path(repo_root, session_date)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")
    return did


def run_retention_job(repo_root: Path, *, config: PlatterPipelineConfig | None = None) -> int:
    """Delete partitions older than retention_days; return bytes freed estimate."""
    cfg = config or load_platter_pipeline_config(repo_root / "config" / "platter_pipeline.yaml")
    cutoff = date.today() - timedelta(days=cfg.telemetry.retention_days)
    freed = 0
    log_path = repo_root / "data" / "telemetry" / "retention.log"
    for root_name in ("ticks", "minute", "features", "agent_decisions"):
        base = repo_root / "data" / ("telemetry" if root_name == "agent_decisions" else "platter")
        if root_name == "agent_decisions":
            base = repo_root / "data" / "telemetry" / "agent_decisions"
        else:
            base = repo_root / "data" / "platter" / root_name
        if not base.is_dir():
            continue
        for child in base.iterdir():
            if not child.is_dir():
                continue
            try:
                part_date = date.fromisoformat(child.name)
            except ValueError:
                continue
            if part_date >= cutoff:
                continue
            for path in child.rglob("*"):
                if path.is_file():
                    freed += path.stat().st_size
            shutil.rmtree(child)
    platters = repo_root / "data" / "platter" / "platters"
    if platters.is_dir():
        for child in platters.iterdir():
            try:
                part_date = date.fromisoformat(child.name)
            except ValueError:
                continue
            if part_date >= cutoff:
                continue
            for path in child.rglob("*"):
                if path.is_file():
                    freed += path.stat().st_size
            shutil.rmtree(child)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "at_utc": datetime.now(tz=_IST).isoformat(),
                    "bytes_freed": freed,
                    "cutoff": cutoff.isoformat(),
                }
            )
            + "\n"
        )
    return freed


def run_weekly_rollup(repo_root: Path, *, week: str | None = None) -> Path:
    """Build agent_week JSON for ISO week string YYYY-Www."""
    today = date.today()
    iso = today.isocalendar()
    week_label = week or f"{iso.year}-W{iso.week:02d}"
    year = int(week_label.split("-W")[0])
    week_num = int(week_label.split("-W")[1])
    decisions: list[dict[str, Any]] = []
    replay_index: dict[str, str] = {}
    base = repo_root / "data" / "telemetry" / "agent_decisions"
    if base.is_dir():
        for session_dir in sorted(base.iterdir()):
            log = session_dir / "decisions.jsonl"
            if not log.is_file():
                continue
            try:
                session = date.fromisoformat(session_dir.name)
            except ValueError:
                continue
            if session.isocalendar()[1] != week_num or session.isocalendar()[0] != year:
                continue
            for line in log.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                decisions.append(row)
                replay_index[str(row["decision_id"])] = str(row["platter_snapshot_path"])
    summary = {
        "long": sum(1 for d in decisions if d.get("agent_output", {}).get("direction") == "LONG"),
        "short": sum(1 for d in decisions if d.get("agent_output", {}).get("direction") == "SHORT"),
        "neutral": sum(
            1 for d in decisions if d.get("agent_output", {}).get("direction") == "NEUTRAL"
        ),
        "veto": sum(1 for d in decisions if d.get("gate_output", {}).get("action") == "VETO"),
    }
    out = {
        "schema_version": "1",
        "week": week_label,
        "summary": summary,
        "decisions": decisions,
        "replay_index": replay_index,
    }
    path = repo_root / "data" / "telemetry" / "weekly" / f"agent_week_{week_label}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    return path
