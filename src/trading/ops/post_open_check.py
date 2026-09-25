"""One-time post-open PAPER readiness check with operator notification."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from trading.domain.clock import Clock, WallClock
from trading.ops.operator_alert import notify_operator
from trading.ops.orchestrator import PAPER_RUNTIME_UNITS
from trading.ops.service_supervisor import ServiceSupervisor, SystemdServiceSupervisor
from trading.runtime.paper_session import load_paper_session_config

__all__ = ["PostOpenCheckResult", "run_post_open_check"]


@dataclass(frozen=True, slots=True)
class PostOpenCheckResult:
    passed: bool
    checks: tuple[tuple[str, bool, str], ...]
    notified: bool


def _json(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} is not a JSON object")
    return payload


def _fresh(raw: object, *, now: datetime, max_age: timedelta) -> bool:
    if not isinstance(raw, str):
        return False
    stamp = datetime.fromisoformat(raw)
    if stamp.tzinfo is None:
        return False
    age = now - stamp
    return timedelta(0) <= age <= max_age


def run_post_open_check(
    repo_root: Path,
    *,
    clock: Clock | None = None,
    supervisor: ServiceSupervisor | None = None,
    send_notification: bool = True,
) -> PostOpenCheckResult:
    """Check four-mode PAPER health and send one dated Telegram result."""
    wall = clock or WallClock()
    now = wall.now_utc()
    checks: list[tuple[str, bool, str]] = []

    cfg = load_paper_session_config(repo_root / "config/paper_session.yaml")
    checks.append(
        (
            "environment",
            cfg.routing_profile.value == "four_mode",
            cfg.routing_profile.value,
        )
    )
    checks.append(("entries", cfg.new_entries_enabled, str(cfg.new_entries_enabled)))
    expected_modes = {
        "M1_CAS",
        "M2_DIRECTIONAL",
        "M3_TACTICAL_POSITIONAL",
        "M4_STRATEGIC_POSITIONAL",
    }
    paper_modes = {
        key for key, value in cfg.mode_stances.items() if value.value == "PAPER"
    }
    checks.append(
        ("four_modes", paper_modes == expected_modes, ",".join(sorted(paper_modes)))
    )

    service_supervisor = supervisor or SystemdServiceSupervisor()
    for unit in PAPER_RUNTIME_UNITS:
        state = service_supervisor.state(unit)
        checks.append((unit, state.active, state.detail))

    try:
        session = _json(repo_root / cfg.session_heartbeat_path)
        session_ok = (
            _fresh(session.get("timestamp"), now=now, max_age=timedelta(seconds=180))
            and session.get("new_entries_enabled") is True
            and session.get("system_state") == "READY"
        )
        checks.append(("session_heartbeat", session_ok, str(session.get("timestamp"))))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        checks.append(("session_heartbeat", False, str(exc)))

    try:
        protection = _json(repo_root / "data/paper/protection_heartbeat.json")
        protection_ok = (
            _fresh(protection.get("as_of"), now=now, max_age=timedelta(seconds=30))
            and protection.get("ws_connected") is True
            and protection.get("protection_degraded") is False
        )
        checks.append(("market_feed", protection_ok, str(protection.get("as_of"))))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        checks.append(("market_feed", False, str(exc)))

    try:
        connection = sqlite3.connect(repo_root / cfg.store_path)
        try:
            integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        finally:
            connection.close()
        checks.append(("trading_store", integrity == "ok", integrity))
    except (OSError, sqlite3.Error) as exc:
        checks.append(("trading_store", False, str(exc)))

    passed = all(ok for _, ok, _ in checks)
    lines = [
        f"{name}={'PASS' if ok else 'FAIL'} ({detail})" for name, ok, detail in checks
    ]
    notified = False
    if send_notification:
        notified = notify_operator(
            repo_root,
            title=f"09:16 PAPER CHECK {'PASS' if passed else 'FAIL'}",
            detail="\n".join(lines),
            dedupe_key=f"post-open:{now.date().isoformat()}",
            cooldown_seconds=86_400,
            clock=wall,
        )
    return PostOpenCheckResult(passed=passed, checks=tuple(checks), notified=notified)
