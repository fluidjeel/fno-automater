# ruff: noqa: E501, PLR0917
"""Build a disposable, read-only operations view from durable evidence.

The dashboard never imports a broker adapter and never mutates trading state.
It deliberately distinguishes observed evidence from configuration and design
intent so that an empty store cannot be mistaken for a healthy live system.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
from collections import Counter
from contextlib import suppress
from datetime import UTC, datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from trading.portfolio.risk_journal import PortfolioRiskJournal
from trading.runtime.family_status import (
    build_family_operator_view,
    build_four_mode_allocations,
)
from trading.runtime.paper_session import load_paper_session_config

_IST = ZoneInfo("Asia/Kolkata")
_SATURDAY = 5
_FRESH_SECONDS = 120
_DEGRADED_SECONDS = 300
_SENSITIVE_FRAGMENTS = ("token", "secret", "password", "credential", "auth_code")
_UNITS = (
    "fno-automated.service",
    "fno-data-pipeline.timer",
    "fno-data-pipeline.service",
    "fno-data-tick.service",
    "fno-fyers-refresh.timer",
    "fno-fyers-refresh.service",
    "fno-paper-session.service",
    "fno-paper-watchdog.timer",
)


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None) -> str | None:
    return (
        None
        if value is None
        else value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    )


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return _redact(loaded) if isinstance(loaded, dict) else {}


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): "[redacted]"
            if any(part in str(key).lower() for part in _SENSITIVE_FRAGMENTS)
            else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _run(command: list[str], *, cwd: Path, timeout: float = 2.0) -> str:
    try:
        result = subprocess.run(  # noqa: S603 - argv only; no shell execution
            command,
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _market_phase(at: datetime) -> dict[str, str]:
    local = at.astimezone(_IST)
    if local.weekday() >= _SATURDAY:
        return {"phase": "CLOSED", "detail": "Weekend · NSE/NFO closed"}
    current = local.time().replace(tzinfo=None)
    if current < time(8, 45):
        return {"phase": "PRE_OPEN", "detail": "Before desk readiness window"}
    if current < time(9, 15):
        return {"phase": "READINESS", "detail": "Authentication and safety checks"}
    if current <= time(15, 30):
        return {"phase": "LIVE", "detail": "NSE/NFO continuous session"}
    if current <= time(15, 40):
        return {"phase": "CLOSE", "detail": "Close and cohort freeze window"}
    return {"phase": "CLOSED", "detail": "Post-market"}


def _file_inventory(root: Path, now: datetime, market_phase: str) -> dict[str, Any]:
    canonical = sorted((root / "data" / "canonical").glob("*.jsonl"))
    snapshots = sorted((root / "data" / "snapshots").glob("*.jsonl"))
    raw = list((root / "data" / "raw").glob("**/*.json"))
    parquet = list((root / "data" / "parquet").glob("*.parquet"))
    latest_candidates = canonical + snapshots + raw
    latest_path = (
        max(latest_candidates, key=lambda path: path.stat().st_mtime)
        if latest_candidates
        else None
    )
    latest_at = (
        datetime.fromtimestamp(latest_path.stat().st_mtime, tz=UTC)
        if latest_path
        else None
    )
    age_seconds = (
        None if latest_at is None else max(0, int((now - latest_at).total_seconds()))
    )
    if latest_at is None:
        freshness = "NO_EVIDENCE"
    elif market_phase != "LIVE":
        freshness = "OFF_HOURS"
    elif age_seconds is not None and age_seconds <= _FRESH_SECONDS:
        freshness = "FRESH"
    elif age_seconds is not None and age_seconds <= _DEGRADED_SECONDS:
        freshness = "DEGRADED"
    else:
        freshness = "STALE"
    return {
        "canonical_files": len(canonical),
        "snapshot_files": len(snapshots),
        "raw_captures": len(raw),
        "parquet_files": len(parquet),
        "instrument_master_files": len(
            list((root / "data" / "reference" / "instruments").glob("*.jsonl"))
        ),
        "latest_at": _iso(latest_at),
        "latest_path": str(latest_path.relative_to(root)) if latest_path else None,
        "age_seconds": age_seconds,
        "freshness": freshness,
        "storage_bytes": sum(
            path.stat().st_size
            for path in canonical + snapshots + raw + parquet
            if path.is_file()
        ),
    }


def _db_snapshot(path: Path) -> dict[str, Any]:
    empty: dict[str, Any] = {
        "present": path.is_file(),
        "events": [],
        "event_counts": {},
        "reservations": [],
        "positions": [],
        "protections": [],
        "reviews": [],
        "system_state": "NO_SESSION_EVIDENCE",
        "last_reconciliation_ref": None,
        "entry_freeze": None,
        "idempotency_key_count": 0,
        "agent_decisions": [],
        "latest_cycle": None,
    }
    if not path.is_file():
        return empty
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1)
        connection.row_factory = sqlite3.Row
    except sqlite3.Error:
        return empty
    try:
        counts = connection.execute(
            "SELECT event_type, COUNT(*) count FROM trading_events GROUP BY event_type"
        ).fetchall()
        empty["event_counts"] = {
            str(row["event_type"]): int(row["count"]) for row in counts
        }
        rows = connection.execute(
            "SELECT sequence,event_id,event_type,payload,idempotency_key,recorded_at "
            "FROM trading_events ORDER BY sequence DESC LIMIT 250"
        ).fetchall()
        events: list[dict[str, Any]] = []
        for row in rows:
            try:
                payload = _redact(json.loads(str(row["payload"])))
            except json.JSONDecodeError:
                payload = {"parse_error": True}
            event_type = str(row["event_type"])
            action = _first(payload, "action", "state", "status", "system_state")
            if event_type == "cycle_evidence":
                route = payload.get("route_decision") or {}
                if isinstance(route, dict):
                    action = route.get("paper_winner") or action
            events.append(
                {
                    "sequence": int(row["sequence"]),
                    "event_id": str(row["event_id"]),
                    "type": event_type,
                    "recorded_at": str(row["recorded_at"]),
                    "idempotency_key": row["idempotency_key"],
                    "trace_id": _first(
                        payload,
                        "trade_id",
                        "intent_id",
                        "decision_id",
                        "order_id",
                        "reservation_id",
                        "cycle_id",
                    ),
                    "action": action,
                    "reasons": payload.get("reason_codes", []),
                    "payload": payload,
                }
            )
        empty["events"] = events
        empty["reservations"] = _json_rows(
            connection, "SELECT payload FROM reservations ORDER BY updated_at DESC"
        )
        empty["positions"] = _json_rows(
            connection,
            "SELECT payload FROM position_lifecycle ORDER BY updated_at DESC",
        )
        empty["protections"] = _json_rows(
            connection, "SELECT payload FROM protection_state ORDER BY updated_at DESC"
        )
        empty["reviews"] = [
            dict(row)
            for row in connection.execute(
                "SELECT slot_id,session_date,venue,as_of FROM review_slot_runs ORDER BY as_of DESC LIMIT 100"
            ).fetchall()
        ]
        row = connection.execute(
            "SELECT state,last_reconciliation_ref,updated_at FROM system_state WHERE singleton=1"
        ).fetchone()
        if row:
            empty["system_state"] = str(row["state"])
            empty["last_reconciliation_ref"] = row["last_reconciliation_ref"]
            empty["system_state_updated_at"] = str(row["updated_at"])
        freeze = connection.execute(
            "SELECT payload FROM entry_freeze WHERE singleton=1"
        ).fetchone()
        if freeze:
            empty["entry_freeze"] = _redact(json.loads(str(freeze["payload"])))
        key_row = connection.execute(
            "SELECT COUNT(*) count FROM idempotency_keys"
        ).fetchone()
        empty["idempotency_key_count"] = int(key_row["count"]) if key_row else 0
        empty["agent_decisions"] = _agent_decisions(connection)
        empty["latest_cycle"] = _latest_cycle_evidence(events)
    except (sqlite3.Error, json.JSONDecodeError):
        empty["read_error"] = "Trading evidence store could not be read"
    finally:
        connection.close()
    return empty


def _agent_decisions(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    try:
        rows = connection.execute(
            "SELECT payload FROM agent_decisions ORDER BY created_at DESC LIMIT 50"
        ).fetchall()
    except sqlite3.Error:
        return []
    decisions: list[dict[str, Any]] = []
    for row in rows:
        try:
            payload = _redact(json.loads(str(row["payload"])))
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            decisions.append(payload)
    return decisions


def _latest_cycle_evidence(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    for event in events:
        if event.get("type") != "cycle_evidence":
            continue
        payload = event.get("payload", {})
        if isinstance(payload, dict):
            return payload
    return None


def _json_rows(connection: sqlite3.Connection, sql: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for row in connection.execute(sql).fetchall():
        try:
            payload = json.loads(str(row["payload"]))
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            result.append(_redact(payload))
    return result


def _first(payload: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = payload.get(key)
        if value is not None:
            return value
    return None


def _systemd(root: Path) -> list[dict[str, Any]]:
    if shutil.which("systemctl") is None:
        return [
            {
                "name": unit,
                "status": "UNOBSERVED",
                "detail": "systemd is unavailable on this host",
            }
            for unit in _UNITS
        ]
    result: list[dict[str, Any]] = []
    properties = "LoadState,ActiveState,SubState,Result,NRestarts,ExecMainStartTimestamp,ExecMainExitTimestamp"
    for unit in _UNITS:
        output = _run(["systemctl", "show", unit, f"--property={properties}"], cwd=root)
        values = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
        load = values.get("LoadState", "not-found")
        active = values.get("ActiveState", "unknown")
        sub = values.get("SubState", "unknown")
        if load == "not-found":
            status = "NOT_INSTALLED"
        elif active == "failed" or values.get("Result") == "failed":
            status = "FAILED"
        elif active == "active" or (
            active == "inactive" and values.get("Result") == "success"
        ):
            status = "RUNNING" if active == "active" else "IDLE"
        else:
            status = "INACTIVE"
        result.append(
            {"name": unit, "status": status, "detail": f"{active}/{sub}", **values}
        )
    return result


def _daemon_heartbeat(root: Path) -> dict[str, Any]:
    return _read_json(root / "data" / "daemon_heartbeat.json")


def _session_heartbeat(root: Path, session: dict[str, Any]) -> dict[str, Any]:
    path = root / str(
        session.get("session_heartbeat_path", "data/paper/session_heartbeat.json")
    )
    return _read_json(path)


def _supervision_view(
    *,
    services: list[dict[str, Any]],
    daemon: dict[str, Any],
    session_hb: dict[str, Any],
    now: datetime,
) -> dict[str, Any]:
    service_map = {item["name"]: item for item in services}
    daemon_service = service_map.get("fno-automated.service", {})
    paper_service = service_map.get("fno-paper-session.service", {})
    watchdog = service_map.get("fno-paper-watchdog.timer", {})
    daemon_age = _heartbeat_age_seconds(daemon.get("timestamp"), now)
    session_age = _heartbeat_age_seconds(session_hb.get("timestamp"), now)
    return {
        "daemon": {
            "phase": daemon.get("phase"),
            "timestamp": daemon.get("timestamp"),
            "local_time": daemon.get("local_time"),
            "age_seconds": daemon_age,
            "state": _heartbeat_state(daemon_age, stale_after=180),
            "service_status": daemon_service.get("status", "UNOBSERVED"),
        },
        "paper_session": {
            "timestamp": session_hb.get("timestamp"),
            "local_time": session_hb.get("local_time"),
            "age_seconds": session_age,
            "state": _heartbeat_state(session_age, stale_after=180),
            "service_status": paper_service.get("status", "UNOBSERVED"),
            "cycle_count": session_hb.get("cycle_count"),
            "route_winner": session_hb.get("route_winner"),
            "open_positions": session_hb.get("open_positions"),
            "system_state": session_hb.get("system_state"),
            "entries_blocked": session_hb.get("entries_blocked"),
        },
        "watchdog": {
            "service_status": watchdog.get("status", "UNOBSERVED"),
            "detail": watchdog.get("detail"),
        },
    }


def _heartbeat_age_seconds(timestamp: str | None, now: datetime) -> int | None:
    if not timestamp:
        return None
    try:
        observed = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=UTC)
    return max(0, int((now.astimezone(UTC) - observed.astimezone(UTC)).total_seconds()))


def _heartbeat_state(age_seconds: int | None, *, stale_after: int) -> str:
    if age_seconds is None:
        return "NO_EVIDENCE"
    if age_seconds <= stale_after:
        return "FRESH"
    if age_seconds <= stale_after * 3:
        return "STALE"
    return "MISSING"


def _cohort_packages(root: Path, session: dict[str, Any]) -> list[dict[str, Any]]:
    cohort_dir = root / str(session.get("cohort_dir", "data/paper/cohorts"))
    if not cohort_dir.is_dir():
        return []
    packages: list[dict[str, Any]] = []
    for path in sorted(cohort_dir.glob("*.json"), reverse=True)[:10]:
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(loaded, dict):
            continue
        experiment = loaded.get("experiment", {})
        signals = loaded.get("signals", [])
        packages.append(
            {
                "file": path.name,
                "experiment_id": experiment.get("experiment_id"),
                "strategy_id": experiment.get("strategy_id"),
                "execution_mode": experiment.get("execution_mode"),
                "observation_start": loaded.get("observation_start"),
                "observation_end": loaded.get("observation_end"),
                "signal_count": len(signals) if isinstance(signals, list) else 0,
                "declined_count": sum(
                    1
                    for item in signals
                    if isinstance(item, dict) and item.get("declined")
                )
                if isinstance(signals, list)
                else 0,
                "package": _redact(loaded),
            }
        )
    return packages


def _agent_runs(root: Path) -> list[dict[str, Any]]:
    run_root = root / "data" / "paper" / "agent_runs"
    runs: list[dict[str, Any]] = []
    for directory in sorted(run_root.glob("*"), reverse=True)[:20]:
        if not directory.is_dir():
            continue
        meta = _read_json(directory / "meta.json")
        proposal = _read_json(directory / "proposal.json")
        advice = _read_json(directory / "advice.json")
        outcome = proposal or advice
        cohort = str(meta.get("cohort", ""))
        runs.append(
            {
                "run_id": directory.name,
                "kind": "WEEKLY_PROPOSAL" if proposal else "STRUCTURE_ADVICE",
                "as_of": outcome.get("as_of_time") or outcome.get("as_of"),
                "result": outcome.get("recommendation")
                or outcome.get("stance")
                or "UNKNOWN",
                "confidence": outcome.get("confidence"),
                "model": meta.get("model"),
                "fixture": "fixtures" in cohort,
                "enabled_override": bool(meta.get("enabled_override", False)),
                "failed_gates": outcome.get("failed_gate_ids", []),
                "missing_data": outcome.get("missing_data", []),
                "narrative": outcome.get("narrative") or "",
            }
        )
    return runs


def _journal(positions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    journal: list[dict[str, Any]] = []
    for record in positions:
        position = record.get("position", {})
        intent = record.get("intent", {})
        decision = record.get("risk_decision", {})
        legs = position.get("legs", []) if isinstance(position, dict) else []
        analysis: list[str] = []
        if decision.get("action") == "RESIZE":
            analysis.append("Layer 2 resized the strategy request before execution.")
        if position.get("protection_degraded"):
            analysis.append(
                "Protection degraded during this trade; inspect quote and monitor continuity."
            )
        if position.get("software_stop_unavailable"):
            analysis.append(
                "Software stop coverage was unavailable; classify as a safety incident."
            )
        reviews = record.get("reviews", [])
        if not reviews:
            analysis.append(
                "No scheduled positional review is recorded for this lifecycle."
            )
        journal.append(
            {
                "trade_id": record.get("trade_id"),
                "strategy": position.get("strategy_id") or intent.get("strategy_id"),
                "mode": position.get("execution_mode") or intent.get("execution_mode"),
                "state": position.get("state"),
                "opened_at": position.get("opened_at"),
                "as_of": record.get("as_of"),
                "action": decision.get("action"),
                "reasons": decision.get("reason_codes", []),
                "reserved_capital": decision.get("reserved_capital"),
                "max_loss": decision.get("recalculated_max_loss"),
                "margin": decision.get("margin_required"),
                "legs": legs,
                "exit_policy": position.get("exit_policy", {}),
                "reviews": reviews,
                "analysis": analysis,
                "evidence": {
                    "intent": intent,
                    "risk_decision": decision,
                    "position": position,
                },
            }
        )
    return journal


def _strategy_rows(session: dict[str, Any], db: dict[str, Any]) -> list[dict[str, Any]]:
    stances = session.get("strategy_stances", {})
    counts: Counter[str] = Counter()
    for position in db["positions"]:
        strategy = position.get("position", {}).get("strategy_id")
        if strategy:
            counts[str(strategy)] += 1
    return [
        {
            "name": strategy,
            "stance": stances.get(strategy, "NOT_CONFIGURED"),
            "executed": stances.get(strategy) == "PAPER",
            "lifecycle_count": counts[strategy],
            "evidence": "OBSERVED" if counts[strategy] else "CONFIG_ONLY",
        }
        for strategy in session.get("strategy_ids", [])
    ]


def _portfolio_risk_view(root: Path, session: dict[str, Any]) -> dict[str, Any]:
    journal_root = root / str(
        session.get("portfolio_risk_dir", "data/paper/portfolio_risk")
    )
    if not journal_root.is_dir():
        return {"state": "MISSING", "latest_at": None}
    latest = PortfolioRiskJournal(journal_root).read_latest()
    if latest is None:
        return {"state": "NO_EVIDENCE", "latest_at": None}
    exposure = latest.exposure
    stress = latest.stress
    dumped = latest.model_dump(mode="json")
    return {
        "state": "OBSERVED",
        "latest_at": _iso(latest.as_of),
        "portfolio_snapshot_id": latest.portfolio_snapshot_id,
        "open_position_count": latest.open_position_count,
        "margin_used": dumped["margin_used"],
        "margin_available": dumped["margin_available"],
        "margin_utilisation_fraction": str(latest.margin_utilisation_fraction),
        "net_delta": exposure.get("net_delta"),
        "net_vega": exposure.get("net_vega"),
        "net_theta": exposure.get("net_theta"),
        "net_gamma": exposure.get("net_gamma"),
        "worst_case": stress.get("worst_case"),
        "worst_case_pct_equity": stress.get("worst_case_pct_equity"),
        "tail_budget_fraction": stress.get("tail_budget_fraction"),
        "breached_budget": stress.get("breached_budget"),
        "scenario_count": len(
            results if isinstance(results := stress.get("results"), list) else []
        ),
    }


def _risk_view(
    base: dict[str, Any], policy: dict[str, Any], db: dict[str, Any]
) -> dict[str, Any]:
    allocations = []
    total = 0.0
    for name, raw in policy.get("strategy_allocations", {}).items():
        fraction = float(raw.get("allocation_fraction", 0))
        total += fraction
        allocations.append(
            {
                "strategy": name,
                "fraction": fraction,
                "percent": round(fraction * 100, 2),
            }
        )
    held = 0.0
    by_state: Counter[str] = Counter()
    for reservation in db["reservations"]:
        state = str(reservation.get("state", "UNKNOWN"))
        by_state[state] += 1
        if state in {"REQUESTED", "RESERVED", "COMMITTED"}:
            with suppress(TypeError, ValueError):
                held += float(reservation.get("amount", {}).get("amount", 0))
    return {
        "limits": base.get("risk", {}),
        "allocations": allocations,
        "allocated_fraction": round(total, 4),
        "unallocated_fraction": round(max(0.0, 1.0 - total), 4),
        "underlying_concentration_fraction": policy.get(
            "underlying_concentration_fraction"
        ),
        "options_premium_budget_fraction": policy.get(
            "options_premium_budget_fraction"
        ),
        "slippage_buffer_fraction": policy.get("slippage_buffer_fraction"),
        "net_delta_limit": policy.get("net_delta_limit"),
        "paper_future_margin_fraction": policy.get("paper_future_margin_fraction"),
        "reservation_count": len(db["reservations"]),
        "reservation_states": dict(by_state),
        "held_capital": held,
        "currency": base.get("base_currency", "INR"),
    }


def _components(
    *,
    files: dict[str, Any],
    db: dict[str, Any],
    services: list[dict[str, Any]],
    session: dict[str, Any],
    agent: dict[str, Any],
    agent_runs: list[dict[str, Any]],
    heartbeat: dict[str, Any],
    news: dict[str, Any],
    supervision: dict[str, Any],
) -> list[dict[str, Any]]:
    service_map = {item["name"]: item for item in services}
    event_counts = db["event_counts"]
    daemon = supervision.get("daemon", {})
    session_hb = supervision.get("paper_session", {})
    layers = [
        {
            "id": "L0",
            "name": "Supervision & autopilot",
            "owner": "operator unattended",
            "components": [
                _component(
                    "Supervisor daemon",
                    daemon.get("state", "NO_EVIDENCE"),
                    "daemon heartbeat + systemd",
                    daemon.get("phase") or daemon.get("service_status"),
                ),
                _component(
                    "Paper session loop",
                    session_hb.get("state", "NO_EVIDENCE"),
                    "session heartbeat + systemd",
                    session_hb.get("system_state") or session_hb.get("service_status"),
                ),
                _component(
                    "Paper watchdog timer",
                    _service_status(service_map.get("fno-paper-watchdog.timer")),
                    "systemd timer",
                    supervision.get("watchdog", {}).get("detail"),
                ),
                _component(
                    "Cycle evidence journal",
                    "OBSERVED" if event_counts.get("cycle_evidence") else "NO_EVIDENCE",
                    "cycle_evidence events",
                    str(event_counts.get("cycle_evidence", 0)),
                ),
            ],
        },
        {
            "id": "L1",
            "name": "Data & foundations",
            "owner": "deterministic",
            "components": [
                _component(
                    "REST market pipeline",
                    _service_status(service_map.get("fno-data-pipeline.timer")),
                    "systemd + raw/canonical files",
                    files["latest_at"],
                ),
                _component(
                    "WebSocket ticks",
                    _service_status(service_map.get("fno-data-tick.service")),
                    "systemd",
                    files["latest_at"],
                ),
                _component(
                    "Instrument master",
                    "OBSERVED" if files["instrument_master_files"] else "NO_EVIDENCE",
                    "reference files",
                    str(files["instrument_master_files"]),
                ),
                _component(
                    "Snapshot builder",
                    files["freshness"],
                    "snapshot store",
                    str(files["snapshot_files"]),
                ),
                _component(
                    "News/event risk",
                    "CONFIGURED" if news.get("sources") else "NOT_CONFIGURED",
                    "news config + JSONL",
                    news.get("sentiment_provider", "unknown"),
                ),
                _component(
                    "P1 IV/Greeks/depth",
                    "PARTIAL",
                    "paper_data policy",
                    "Observed-only; CAS blocks without depth",
                ),
            ],
        },
        {
            "id": "L2",
            "name": "Risk, execution & state",
            "owner": "sole live authority",
            "components": [
                _component(
                    "Broker reconciliation",
                    "OBSERVED"
                    if event_counts.get("reconciliation_event")
                    else "NO_EVIDENCE",
                    "trading_events",
                    db["last_reconciliation_ref"],
                ),
                _component(
                    "Risk gateway & sizing",
                    "OBSERVED" if event_counts.get("risk_decision") else "CONFIGURED",
                    "risk_decision events",
                    str(event_counts.get("risk_decision", 0)),
                ),
                _component(
                    "Capital reservation CAS",
                    "OBSERVED" if db["reservations"] else "CONFIGURED",
                    "reservations table",
                    str(len(db["reservations"])),
                ),
                _component(
                    "OMS + idempotency",
                    "OBSERVED" if event_counts.get("order_event") else "CONFIGURED",
                    "orders + unique keys",
                    str(db["idempotency_key_count"]),
                ),
                _component(
                    "Portfolio lifecycle",
                    "OBSERVED" if db["positions"] else "NO_EVIDENCE",
                    "position_lifecycle",
                    str(len(db["positions"])),
                ),
                _component(
                    "Protection monitor",
                    _heartbeat_status(heartbeat),
                    "heartbeat + protection_state",
                    heartbeat.get("as_of"),
                ),
            ],
        },
        {
            "id": "L3",
            "name": "Strategy systems",
            "owner": "intent only",
            "components": [
                _component(
                    row["name"],
                    row["stance"],
                    "paper_session strategy stance",
                    f"{row['lifecycle_count']} lifecycle(s)",
                )
                for row in _strategy_rows(session, db)
            ],
        },
        {
            "id": "L4",
            "name": "Evaluation & advisory agents",
            "owner": "proposal only",
            "components": [
                _component(
                    "Deterministic scorecards",
                    "CONFIGURED",
                    "cohort evaluator",
                    "Forward evidence only",
                ),
                _component(
                    "Promotion eligibility",
                    "BLOCKED",
                    "evaluation policy",
                    "Human promotion record required",
                ),
                _component(
                    "Weekly strategy-family agent",
                    "DISABLED" if not agent.get("enabled") else "ENABLED",
                    "agent config",
                    f"{len(agent_runs)} recorded run(s)",
                ),
                _component(
                    "Post-trade analyst",
                    "WAITING" if not db["positions"] else "OBSERVED",
                    "journal lifecycle evidence",
                    f"{len(db['positions'])} trade(s)",
                ),
            ],
        },
    ]
    return layers


def _component(name: str, status: str, evidence: str, detail: Any) -> dict[str, Any]:
    return {"name": name, "status": status, "evidence": evidence, "detail": detail}


def _service_status(service: dict[str, Any] | None) -> str:
    return "UNOBSERVED" if service is None else str(service.get("status", "UNOBSERVED"))


def _heartbeat_status(heartbeat: dict[str, Any]) -> str:
    if not heartbeat:
        return "NO_EVIDENCE"
    if heartbeat.get("protection_degraded"):
        return "DEGRADED"
    return "OBSERVED" if heartbeat.get("monitor_active") else "INACTIVE"


def _protection_ws_finding(
    heartbeat: dict[str, Any],
    *,
    market_phase: str,
) -> dict[str, str] | None:
    if not heartbeat or heartbeat.get("ws_connected"):
        return None
    open_positions = int(heartbeat.get("open_positions", 0))
    if open_positions > 0:
        severity = "HIGH"
        detail = "Exposure exists while the real-time protection channel is down."
    elif market_phase == "LIVE":
        severity = "MEDIUM"
        detail = (
            "Market is live but the protection WebSocket is disconnected; "
            "reconnect before taking exposure."
        )
    else:
        severity = "INFO"
        detail = (
            "No open position is reported and the market is closed; verify "
            "reconnection before the next live session."
        )
    return {
        "severity": severity,
        "area": "Protection",
        "title": "Protection heartbeat reports WebSocket disconnected",
        "detail": detail,
    }


def _findings(
    *,
    files: dict[str, Any],
    db: dict[str, Any],
    services: list[dict[str, Any]],
    agent: dict[str, Any],
    evaluation: dict[str, Any],
    heartbeat: dict[str, Any],
    portfolio_risk: dict[str, Any],
    supervision: dict[str, Any],
    market_phase: str,
) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    paper_service = next(
        (item for item in services if item["name"] == "fno-paper-session.service"), {}
    )
    session_hb = supervision.get("paper_session", {})
    if (
        paper_service.get("status") in {"NOT_INSTALLED", "UNOBSERVED"}
        and session_hb.get("state") == "NO_EVIDENCE"
    ):
        findings.append(
            {
                "severity": "HIGH",
                "area": "Runtime",
                "title": "Paper session is not independently supervised",
                "detail": "No fno-paper-session.service or session heartbeat evidence exists. A terminal disconnect or process crash can stop strategy, protection and journaling loops.",
            }
        )
    daemon_hb = supervision.get("daemon", {})
    if daemon_hb.get("state") in {"STALE", "MISSING", "NO_EVIDENCE"}:
        findings.append(
            {
                "severity": "HIGH"
                if market_phase in {"LIVE", "READINESS"}
                else "MEDIUM",
                "area": "Runtime",
                "title": "Supervisor daemon heartbeat is stale or missing",
                "detail": (
                    f"Latest daemon evidence: {daemon_hb.get('timestamp') or 'none'}. "
                    "fno-automated.service may be down or not writing heartbeats."
                ),
            }
        )
    if session_hb.get("state") in {"STALE", "MISSING"} and market_phase == "LIVE":
        findings.append(
            {
                "severity": "HIGH",
                "area": "Runtime",
                "title": "Paper session heartbeat is stale during the live session",
                "detail": (
                    f"Latest session tick: {session_hb.get('timestamp') or 'none'}. "
                    "Strategy, protection and journaling loops may have stopped."
                ),
            }
        )
    for service in services:
        if service.get("status") != "FAILED":
            continue
        findings.append(
            {
                "severity": "HIGH",
                "area": "Runtime",
                "title": f"{service['name']} is failed",
                "detail": (
                    f"systemd reports {service.get('detail', 'failed')}; inspect the "
                    "unit journal and its last exit before the next required run."
                ),
            }
        )
    ws_finding = _protection_ws_finding(heartbeat, market_phase=market_phase)
    if ws_finding is not None:
        findings.append(ws_finding)
    if not db["events"]:
        findings.append(
            {
                "severity": "MEDIUM",
                "area": "Traceability",
                "title": "Trading event store contains no decision evidence",
                "detail": "No risk, order, reconciliation, reservation or lifecycle events can currently be traced on this source.",
            }
        )
    if not agent.get("enabled"):
        findings.append(
            {
                "severity": "INFO",
                "area": "Agent",
                "title": "Weekly and post-trade LLM analysis is disabled by policy",
                "detail": "Recorded runs may be fixtures or explicit overrides. Deterministic scorecards remain authoritative.",
            }
        )
    if market_phase == "LIVE" and files["freshness"] in {"STALE", "NO_EVIDENCE"}:
        findings.append(
            {
                "severity": "CRITICAL",
                "area": "Data",
                "title": "Market data evidence is stale during the live session",
                "detail": f"Latest observed artifact: {files.get('latest_at') or 'none'}. New entries should remain blocked.",
            }
        )
    backlog: list[dict[str, str]] = []
    if portfolio_risk.get("state") not in {"OBSERVED"}:
        backlog.append(
            {
                "severity": "HIGH",
                "area": "Risk",
                "title": "Portfolio Greeks and scenario P&L are not durably journaled",
                "detail": "The code checks per-trade structures, but the evidence store does not persist a portfolio Greek surface, correlated shock grid, or margin headroom time series.",
            }
        )
    backlog.extend(
        [
            {
                "severity": "MEDIUM",
                "area": "Execution",
                "title": "Latency and queueing stages lack histograms",
                "detail": "Capture feed→snapshot→intent→risk→submit→ack→fill latency, broker rejects, partial fills, amendments, cancellations, and legging duration.",
            },
            {
                "severity": "MEDIUM",
                "area": "Race control",
                "title": "CAS conflicts and duplicate suppression are only inferable after failure",
                "detail": "Persist reservation-CAS attempts, idempotency hits, competing owner, lock wait, and retry outcome as explicit operational events.",
            },
            {
                "severity": "MEDIUM",
                "area": "Model risk",
                "title": "Decision-time IV/Greeks lineage needs full provenance",
                "detail": "Persist vendor/model, timestamp, units, spot/rate/dividend inputs, convergence, and stale/invalid flags for every selected contract.",
            },
        ]
    )
    findings.extend(backlog)
    order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "INFO": 3}
    return sorted(findings, key=lambda item: order[item["severity"]])


def _host(root: Path) -> dict[str, Any]:
    disk = shutil.disk_usage(root)
    memory: dict[str, Any] = {}
    meminfo = Path("/proc/meminfo")
    if meminfo.is_file():
        values: dict[str, int] = {}
        for line in meminfo.read_text(encoding="utf-8").splitlines():
            key, _, raw = line.partition(":")
            try:
                values[key] = int(raw.strip().split()[0]) * 1024
            except (ValueError, IndexError):
                continue
        memory = {
            "total": values.get("MemTotal"),
            "available": values.get("MemAvailable"),
        }
    try:
        load_average = [round(value, 2) for value in os.getloadavg()]
    except OSError:
        load_average = []
    return {
        "hostname": os.uname().nodename,
        "load_average": load_average,
        "disk_total": disk.total,
        "disk_free": disk.free,
        "memory": memory,
    }


def build_dashboard_snapshot(
    repo_root: Path, *, source: str = "local"
) -> dict[str, Any]:
    """Collect one redacted dashboard snapshot from repository and host state."""
    root = repo_root.resolve()
    now = _now()
    phase = _market_phase(now)
    base = _read_yaml(root / "config" / "paper.yaml")
    risk = _read_yaml(root / "config" / "risk.yaml")
    session = _read_yaml(root / "config" / "paper_session.yaml")
    agent = _read_yaml(root / "config" / "agent.yaml")
    evaluation = _read_yaml(root / "config" / "evaluation.yaml")
    paper_data = _read_yaml(root / "config" / "paper_data.yaml")
    identification = _read_yaml(root / "config" / "identification.yaml")
    news = _read_yaml(root / "config" / "news.yaml")
    files = _file_inventory(root, now, phase["phase"])
    db = _db_snapshot(
        root / str(session.get("store_path", "data/paper/trading.sqlite"))
    )
    services = _systemd(root)
    heartbeat = _read_json(root / "data" / "paper" / "protection_heartbeat.json")
    daemon_hb = _daemon_heartbeat(root)
    session_hb = _session_heartbeat(root, session)
    supervision = _supervision_view(
        services=services,
        daemon=daemon_hb,
        session_hb=session_hb,
        now=now,
    )
    portfolio_risk = _portfolio_risk_view(root, session)
    agent_runs = _agent_runs(root)
    cohorts = _cohort_packages(root, session)
    strategies = _strategy_rows(session, db)
    journal = _journal(db["positions"])
    git_revision = _run(["git", "rev-parse", "--short", "HEAD"], cwd=root)
    dirty = (
        bool(_run(["git", "status", "--porcelain"], cwd=root)) if git_revision else None
    )
    findings = _findings(
        files=files,
        db=db,
        services=services,
        agent=agent,
        evaluation=evaluation,
        heartbeat=heartbeat,
        portfolio_risk=portfolio_risk,
        supervision=supervision,
        market_phase=phase["phase"],
    )
    if not git_revision:
        findings.append(
            {
                "severity": "MEDIUM",
                "area": "Governance",
                "title": "Runtime code revision is not identifiable",
                "detail": (
                    "The deployed Oracle directory has no readable Git revision. "
                    "Persist a release ID or source checksum on every deployment."
                ),
            }
        )
    active_positions = sum(
        1
        for item in journal
        if item.get("state") not in {"CLOSED", "CANCELLED", "REJECTED"}
    )
    family_rows: list[dict[str, Any]] = []
    try:
        session_cfg = load_paper_session_config(root / "config" / "paper_session.yaml")
        family_rows = [
            row.model_dump(mode="json")
            for row in build_family_operator_view(session_cfg)
        ]
    except (ValueError, OSError):
        family_rows = []
    return {
        "schema_version": "1",
        "generated_at": _iso(now),
        "source": source,
        "mode": base.get("environment", "UNKNOWN"),
        "account_id": base.get("account_id", "UNKNOWN"),
        "market": phase,
        "build": {"revision": git_revision or "unknown", "dirty": dirty},
        "host": _host(root),
        "summary": {
            "system_state": db["system_state"],
            "entry_gate": "BLOCKED"
            if (db["entry_freeze"] or {}).get("entries_blocked")
            else ("NO_EVIDENCE" if db["entry_freeze"] is None else "OPEN"),
            "data_freshness": files["freshness"],
            "open_positions": active_positions,
            "trading_events": sum(db["event_counts"].values()),
            "critical_findings": sum(
                item["severity"] in {"CRITICAL", "HIGH"} for item in findings
            ),
            "protection": _heartbeat_status(heartbeat),
        },
        "layers": _components(
            files=files,
            db=db,
            services=services,
            session=session,
            agent=agent,
            agent_runs=agent_runs,
            heartbeat=heartbeat,
            news=news,
            supervision=supervision,
        ),
        "supervision": supervision,
        "services": services,
        "data_pipeline": {
            **files,
            "requirements_version": paper_data.get("requirements_version"),
            "requirements": paper_data.get("fields", []),
            "news_sentiment_provider": news.get("sentiment_provider"),
            "news_sources": [
                {
                    "id": item.get("source_id"),
                    "enabled": item.get("enabled"),
                    "tier": item.get("tier"),
                }
                for item in news.get("sources", [])
            ],
        },
        "risk": _risk_view(base, risk, db),
        "portfolio": {
            "positions": db["positions"],
            "protections": db["protections"],
            "heartbeat": heartbeat,
            "risk": portfolio_risk,
        },
        "execution": {
            "event_counts": db["event_counts"],
            "reservations": db["reservations"],
            "idempotency_key_count": db["idempotency_key_count"],
            "reviews": db["reviews"],
            "events": db["events"],
        },
        "strategies": strategies,
        "selection": {
            "identification_versions": {
                key: identification.get(key)
                for key in (
                    "policy_version",
                    "feature_version",
                    "binding_version",
                    "router_version",
                )
            },
            "contract_policy": identification.get("contracts", {}),
            "router_policy": identification.get("router", {}),
            "allow_rules": identification.get("allow_table", {}).get("rules", []),
        },
        "agent": {
            "enabled": bool(agent.get("enabled")),
            "model": agent.get("model"),
            "max_iterations": agent.get("max_iterations"),
            "monthly_budget_inr": agent.get("monthly_budget_inr"),
            "runs": agent_runs,
            "decisions": db["agent_decisions"],
            "authority": "ADVISORY_ONLY",
        },
        "cohorts": cohorts,
        "journal": journal,
        "trace": db["events"],
        "latest_cycle": db["latest_cycle"],
        "four_mode": {
            "allocations": list(build_four_mode_allocations()),
            "families": family_rows,
            "funnel": (db["latest_cycle"] or {}).get("funnel"),
        },
        "findings": findings,
        "coverage": _coverage(
            files,
            db,
            services,
            heartbeat,
            agent,
            portfolio_risk,
            supervision,
        ),
    }


def _coverage(
    files: dict[str, Any],
    db: dict[str, Any],
    services: list[dict[str, Any]],
    heartbeat: dict[str, Any],
    agent: dict[str, Any],
    portfolio_risk: dict[str, Any],
    supervision: dict[str, Any],
) -> list[dict[str, str]]:
    service_map = {item["name"]: item for item in services}
    session_hb = supervision.get("paper_session", {})
    daemon_hb = supervision.get("daemon", {})
    return [
        {
            "area": "Supervisor daemon",
            "state": "COVERED"
            if daemon_hb.get("state") == "FRESH"
            else daemon_hb.get("state", "MISSING"),
            "evidence": daemon_hb.get("timestamp") or "No daemon heartbeat",
        },
        {
            "area": "Host/process health",
            "state": "PARTIAL",
            "evidence": "systemd service state, load, disk; no CPU/memory time series",
        },
        {
            "area": "Market-data freshness",
            "state": "COVERED" if files["latest_at"] else "MISSING",
            "evidence": files.get("latest_at") or "No artifacts",
        },
        {
            "area": "Paper session liveness",
            "state": "COVERED"
            if session_hb.get("state") == "FRESH"
            else (
                "PARTIAL"
                if service_map.get("fno-paper-session.service", {}).get("status")
                not in {"NOT_INSTALLED", "UNOBSERVED"}
                else session_hb.get("state", "MISSING")
            ),
            "evidence": session_hb.get("timestamp")
            or service_map.get("fno-paper-session.service", {}).get("status", "none"),
        },
        {
            "area": "Identification cycle trace",
            "state": "COVERED"
            if db["event_counts"].get("cycle_evidence")
            else "NO_EVIDENCE",
            "evidence": f"{db['event_counts'].get('cycle_evidence', 0)} cycle events",
        },
        {
            "area": "Risk decision lineage",
            "state": "COVERED"
            if db["event_counts"].get("risk_decision")
            else "NO_EVIDENCE",
            "evidence": f"{db['event_counts'].get('risk_decision', 0)} events",
        },
        {
            "area": "Order/fill lineage",
            "state": "COVERED"
            if db["event_counts"].get("order_event")
            else "NO_EVIDENCE",
            "evidence": f"{db['event_counts'].get('order_event', 0)} events",
        },
        {
            "area": "Reservation races",
            "state": "PARTIAL",
            "evidence": "Current state + CAS exceptions; attempt/lock metrics absent",
        },
        {
            "area": "Protection continuity",
            "state": "PARTIAL" if heartbeat else "MISSING",
            "evidence": heartbeat.get("as_of", "No heartbeat"),
        },
        {
            "area": "Portfolio Greeks/scenarios",
            "state": "COVERED"
            if portfolio_risk.get("state") == "OBSERVED"
            else portfolio_risk.get("state", "MISSING"),
            "evidence": portfolio_risk.get("latest_at")
            or "No durable aggregate Greek or shock snapshots",
        },
        {
            "area": "Execution latency",
            "state": "MISSING",
            "evidence": "No stage histograms or broker round-trip series",
        },
        {
            "area": "Trade journal",
            "state": "COVERED" if db["positions"] else "NO_EVIDENCE",
            "evidence": f"{len(db['positions'])} lifecycle records",
        },
        {
            "area": "Post-trade agent",
            "state": "DISABLED" if not agent.get("enabled") else "COVERED",
            "evidence": "Agent policy is proposal-only",
        },
        {
            "area": "Config/doc drift",
            "state": "PARTIAL",
            "evidence": "Charge verification mismatch detected; general semantic diff not yet automated",
        },
    ]
