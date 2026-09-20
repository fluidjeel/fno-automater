"""Tests for unattended daemon supervisor."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from trading.domain.clock import FrozenClock
from trading.ops.daemon import (
    DaemonPhase,
    DaemonSchedule,
    DaemonSupervisor,
    determine_phase,
)

IST = ZoneInfo("Asia/Kolkata")


def test_determine_phase_schedule() -> None:
    schedule = DaemonSchedule()

    # Monday 08:30 IST -> IDLE
    t_idle = datetime(2026, 9, 21, 8, 30, tzinfo=IST)
    assert determine_phase(t_idle, schedule) is DaemonPhase.IDLE

    # Monday 09:05 IST -> PRE_MARKET
    t_pre = datetime(2026, 9, 21, 9, 5, tzinfo=IST)
    assert determine_phase(t_pre, schedule) is DaemonPhase.PRE_MARKET

    # Monday 11:30 IST -> MARKET_ACTIVE
    t_market = datetime(2026, 9, 21, 11, 30, tzinfo=IST)
    assert determine_phase(t_market, schedule) is DaemonPhase.MARKET_ACTIVE

    # Monday 15:32 IST -> POST_MARKET
    t_post = datetime(2026, 9, 21, 15, 32, tzinfo=IST)
    assert determine_phase(t_post, schedule) is DaemonPhase.POST_MARKET

    # Monday 17:00 IST -> IDLE
    t_eod = datetime(2026, 9, 21, 17, 0, tzinfo=IST)
    assert determine_phase(t_eod, schedule) is DaemonPhase.IDLE

    # Saturday 11:00 IST -> IDLE (Market closed)
    t_sat = datetime(2026, 9, 26, 11, 0, tzinfo=IST)
    assert determine_phase(t_sat, schedule) is DaemonPhase.IDLE

    # Sunday 16:30 IST -> WEEKLY_RESEARCH
    t_sun_research = datetime(2026, 9, 27, 16, 30, tzinfo=IST)
    assert determine_phase(t_sun_research, schedule) is DaemonPhase.WEEKLY_RESEARCH


def test_daemon_heartbeat_and_tick(tmp_path: Path) -> None:
    hb_file = tmp_path / "heartbeat.json"
    clock = FrozenClock(datetime(2026, 9, 21, 10, 0, tzinfo=IST).astimezone(UTC))

    transitions: list[tuple[DaemonPhase, DaemonPhase]] = []

    def on_change(old_p: DaemonPhase, new_p: DaemonPhase) -> None:
        transitions.append((old_p, new_p))

    supervisor = DaemonSupervisor(
        clock=clock,
        heartbeat_path=hb_file,
        on_phase_change=on_change,
    )

    phase = supervisor.tick()
    assert phase is DaemonPhase.MARKET_ACTIVE
    assert supervisor.current_phase is DaemonPhase.MARKET_ACTIVE
    assert len(transitions) == 1
    assert transitions[0] == (DaemonPhase.IDLE, DaemonPhase.MARKET_ACTIVE)

    # Verify heartbeat file was written
    assert hb_file.exists()
    data = json.loads(hb_file.read_text(encoding="utf-8"))
    assert data["phase"] == "MARKET_ACTIVE"
    assert "timestamp" in data

    # Stop daemon
    supervisor.stop()
    assert supervisor.current_phase.value == DaemonPhase.STOPPED.value
    data_stopped = json.loads(hb_file.read_text(encoding="utf-8"))
    assert data_stopped["phase"] == "STOPPED"
