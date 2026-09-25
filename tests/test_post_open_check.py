"""09:16 PAPER readiness checklist and notification gate."""

from __future__ import annotations

import json
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from trading.domain.clock import FrozenClock
from trading.ops.post_open_check import run_post_open_check
from trading.ops.service_supervisor import ServiceState

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 25, 3, 46, 5, tzinfo=UTC)


@dataclass
class _Supervisor:
    active: bool = True

    def state(self, unit: str) -> ServiceState:
        return ServiceState(unit, self.active, True, "active/enabled")

    def ensure_active(self, unit: str) -> ServiceState:
        return self.state(unit)


def _root(tmp_path: Path) -> Path:
    (tmp_path / "config").mkdir()
    (tmp_path / "data/paper").mkdir(parents=True)
    shutil.copy(
        ROOT / "config/paper_session.yaml", tmp_path / "config/paper_session.yaml"
    )
    (tmp_path / "data/paper/session_heartbeat.json").write_text(
        json.dumps(
            {
                "timestamp": NOW.isoformat(),
                "new_entries_enabled": True,
                "system_state": "READY",
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "data/paper/protection_heartbeat.json").write_text(
        json.dumps(
            {
                "as_of": NOW.isoformat(),
                "open_positions": 0,
                "monitor_active": True,
                "ws_connected": True,
                "protection_degraded": False,
            }
        ),
        encoding="utf-8",
    )
    connection = sqlite3.connect(tmp_path / "data/paper/trading.sqlite")
    connection.close()
    return tmp_path


def test_post_open_check_passes_all_entry_prerequisites(tmp_path: Path) -> None:
    result = run_post_open_check(
        _root(tmp_path),
        clock=FrozenClock(NOW),
        supervisor=_Supervisor(),
        send_notification=False,
    )
    assert result.passed
    assert all(passed for _, passed, _ in result.checks)


def test_post_open_check_fails_stale_or_disconnected_feed(tmp_path: Path) -> None:
    root = _root(tmp_path)
    heartbeat = root / "data/paper/protection_heartbeat.json"
    payload = json.loads(heartbeat.read_text(encoding="utf-8"))
    payload["ws_connected"] = False
    heartbeat.write_text(json.dumps(payload), encoding="utf-8")

    result = run_post_open_check(
        root,
        clock=FrozenClock(NOW),
        supervisor=_Supervisor(),
        send_notification=False,
    )
    assert not result.passed
    assert ("market_feed", False) in {
        (name, passed) for name, passed, _ in result.checks
    }
