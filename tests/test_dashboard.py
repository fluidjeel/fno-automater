from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import yaml

from trading.dashboard import collector
from trading.dashboard.server import _remote_path


def _write_yaml(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")


def _make_repo(root: Path) -> None:
    _write_yaml(
        root / "config" / "paper.yaml",
        {
            "environment": "PAPER",
            "account_id": "ACC-TEST",
            "base_currency": "INR",
            "risk": {"max_concurrent_trades": 3},
        },
    )
    _write_yaml(
        root / "config" / "paper_session.yaml",
        {
            "store_path": "data/paper/trading.sqlite",
            "portfolio_risk_dir": "data/paper/portfolio_risk",
            "strategy_ids": ["positional_long_option", "debit_spread"],
            "strategy_stances": {
                "positional_long_option": "PAPER",
                "debit_spread": "SHADOW",
            },
        },
    )
    _write_yaml(
        root / "config" / "risk.yaml",
        {
            "strategy_allocations": {
                "positional_long_option": {"allocation_fraction": "0.20"},
                "debit_spread": {"allocation_fraction": "0.05"},
            }
        },
    )
    _write_yaml(root / "config" / "agent.yaml", {"enabled": False})
    _write_yaml(root / "config" / "evaluation.yaml", {})
    _write_yaml(root / "config" / "paper_data.yaml", {"fields": []})
    _write_yaml(root / "config" / "identification.yaml", {})
    _write_yaml(root / "config" / "news.yaml", {})
    db_path = root / "data" / "paper" / "trading.sqlite"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        CREATE TABLE trading_events (
          sequence INTEGER PRIMARY KEY, event_id TEXT, event_type TEXT,
          payload TEXT, idempotency_key TEXT, recorded_at TEXT
        );
        CREATE TABLE reservations (payload TEXT, updated_at TEXT);
        CREATE TABLE position_lifecycle (payload TEXT, updated_at TEXT);
        CREATE TABLE protection_state (payload TEXT, updated_at TEXT);
        CREATE TABLE review_slot_runs (
          slot_id TEXT, session_date TEXT, venue TEXT, as_of TEXT
        );
        CREATE TABLE system_state (
          singleton INTEGER, state TEXT, last_reconciliation_ref TEXT,
          updated_at TEXT
        );
        CREATE TABLE entry_freeze (singleton INTEGER, payload TEXT);
        CREATE TABLE idempotency_keys (idempotency_key TEXT);
        """
    )
    connection.execute(
        "INSERT INTO trading_events VALUES (1,?,?,?,?,?)",
        (
            "EVT-1",
            "risk_decision",
            json.dumps(
                {
                    "decision_id": "DEC-1",
                    "action": "REJECT",
                    "reason_codes": ["DATA_STALE"],
                    "access_token": "must-not-leak",
                }
            ),
            None,
            "2026-09-20T05:00:00Z",
        ),
    )
    connection.commit()
    connection.close()


def test_snapshot_separates_observed_from_configured(
    tmp_path: Path, monkeypatch: Any
) -> None:
    _make_repo(tmp_path)
    monkeypatch.setattr(collector, "_systemd", lambda _root: [])
    monkeypatch.setattr(collector, "_run", lambda *_args, **_kwargs: "testrev")
    monkeypatch.setattr(
        collector,
        "_host",
        lambda _root: {"hostname": "test", "load_average": [0, 0, 0]},
    )

    snapshot = collector.build_dashboard_snapshot(tmp_path, source="test")

    assert snapshot["source"] == "test"
    assert snapshot["summary"]["trading_events"] == 1
    assert snapshot["execution"]["event_counts"] == {"risk_decision": 1}
    assert snapshot["strategies"][0]["evidence"] == "CONFIG_ONLY"
    assert snapshot["risk"]["allocated_fraction"] == 0.25
    assert snapshot["risk"]["unallocated_fraction"] == 0.75
    assert snapshot["trace"][0]["payload"]["access_token"] == "[redacted]"


def test_empty_store_is_not_reported_as_healthy(
    tmp_path: Path, monkeypatch: Any
) -> None:
    _make_repo(tmp_path)
    db_path = tmp_path / "data" / "paper" / "trading.sqlite"
    connection = sqlite3.connect(db_path)
    connection.execute("DELETE FROM trading_events")
    connection.commit()
    connection.close()
    monkeypatch.setattr(
        collector,
        "_systemd",
        lambda _root: [
            {
                "name": "fno-data-pipeline.service",
                "status": "FAILED",
                "detail": "failed/failed",
            }
        ],
    )
    monkeypatch.setattr(collector, "_run", lambda *_args, **_kwargs: "")

    snapshot = collector.build_dashboard_snapshot(tmp_path)

    assert snapshot["summary"]["system_state"] == "NO_SESSION_EVIDENCE"
    assert snapshot["summary"]["entry_gate"] == "NO_EVIDENCE"
    assert any(
        finding["title"] == "Trading event store contains no decision evidence"
        for finding in snapshot["findings"]
    )
    assert any(
        finding["title"] == "fno-data-pipeline.service is failed"
        for finding in snapshot["findings"]
    )


def test_remote_path_preserves_home_expansion() -> None:
    assert _remote_path("~/fno-automated") == '"$HOME"/fno-automated'
    assert _remote_path("/srv/trading desk") == "'/srv/trading desk'"


def test_snapshot_includes_supervision_and_cycle_evidence(
    tmp_path: Path, monkeypatch: Any
) -> None:
    _make_repo(tmp_path)
    daemon_path = tmp_path / "data" / "daemon_heartbeat.json"
    daemon_path.parent.mkdir(parents=True, exist_ok=True)
    daemon_path.write_text(
        json.dumps(
            {
                "timestamp": "2026-09-22T05:00:00+00:00",
                "phase": "MARKET_ACTIVE",
            }
        ),
        encoding="utf-8",
    )
    session_hb = tmp_path / "data" / "paper" / "session_heartbeat.json"
    session_hb.parent.mkdir(parents=True, exist_ok=True)
    session_hb.write_text(
        json.dumps(
            {
                "timestamp": "2026-09-22T05:01:00+00:00",
                "cycle_count": 2,
                "route_winner": "debit_spread",
                "system_state": "READY",
            }
        ),
        encoding="utf-8",
    )
    db_path = tmp_path / "data" / "paper" / "trading.sqlite"
    connection = sqlite3.connect(db_path)
    connection.execute(
        "INSERT INTO trading_events VALUES (2,?,?,?,?,?)",
        (
            "CYC-1",
            "cycle_evidence",
            json.dumps(
                {
                    "schema_version": "1",
                    "cycle_id": "CYC-1",
                    "as_of": "2026-09-22T05:01:00+00:00",
                    "system_state": "READY",
                    "entries_blocked": False,
                    "reconcile_id": "REC-1",
                    "route_decision": {
                        "schema_version": "1",
                        "route_id": "R-1",
                        "router_version": "router-v1",
                        "market_state_id": "MS-1",
                        "paper_winner": "debit_spread",
                    },
                    "strategies": [],
                }
            ),
            None,
            "2026-09-22T05:01:00Z",
        ),
    )
    connection.commit()
    connection.close()
    monkeypatch.setattr(collector, "_systemd", lambda _root: [])
    monkeypatch.setattr(collector, "_run", lambda *_args, **_kwargs: "testrev")
    monkeypatch.setattr(
        collector,
        "_host",
        lambda _root: {"hostname": "test", "load_average": [0, 0, 0]},
    )

    snapshot = collector.build_dashboard_snapshot(tmp_path, source="test")

    assert snapshot["supervision"]["daemon"]["phase"] == "MARKET_ACTIVE"
    assert snapshot["supervision"]["paper_session"]["route_winner"] == "debit_spread"
    assert snapshot["latest_cycle"]["cycle_id"] == "CYC-1"
    assert any(layer["id"] == "L0" for layer in snapshot["layers"])
    assert snapshot["execution"]["event_counts"]["cycle_evidence"] == 1
