"""Unattended process supervisor and market session daemon.

Orchestrates pre-market checks, continuous market trading sessions,
post-market settlement flushes, and weekly research runs according to the
Asia/Kolkata exchange timetable.
"""

from __future__ import annotations

import json
import logging
import signal
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from datetime import time as dt_time
from enum import StrEnum, unique
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from trading.domain.clock import Clock, WallClock

__all__ = [
    "DaemonPhase",
    "DaemonSchedule",
    "DaemonSupervisor",
    "determine_phase",
]

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")


@unique
class DaemonPhase(StrEnum):
    """Operational phase of the autonomous trading daemon."""

    IDLE = "IDLE"
    PRE_MARKET = "PRE_MARKET"
    MARKET_ACTIVE = "MARKET_ACTIVE"
    POST_MARKET = "POST_MARKET"
    WEEKLY_RESEARCH = "WEEKLY_RESEARCH"
    STOPPED = "STOPPED"


@dataclass(frozen=True, slots=True)
class DaemonSchedule:
    """Configured timetable for Indian market operations."""

    pre_market: dt_time = dt_time(9, 0)
    market_open: dt_time = dt_time(9, 15)
    market_close: dt_time = dt_time(15, 30)
    post_market: dt_time = dt_time(15, 35)
    weekly_research_hour: int = 16


SATURDAY = 5
SUNDAY = 6


def determine_phase(
    now: datetime,
    schedule: DaemonSchedule | None = None,
) -> DaemonPhase:
    """Determine the operational phase for a given timestamp in Asia/Kolkata."""
    sched = schedule or DaemonSchedule()
    local = now.astimezone(IST)
    weekday = local.weekday()
    t = local.time()

    if weekday == SUNDAY and t.hour >= sched.weekly_research_hour:
        return DaemonPhase.WEEKLY_RESEARCH
    if weekday in (SATURDAY, SUNDAY):
        return DaemonPhase.IDLE

    if sched.pre_market <= t < sched.market_open:
        return DaemonPhase.PRE_MARKET
    if sched.market_open <= t <= sched.market_close:
        return DaemonPhase.MARKET_ACTIVE
    if sched.market_close < t <= sched.post_market:
        return DaemonPhase.POST_MARKET
    return DaemonPhase.IDLE


class DaemonSupervisor:
    """Supervises unattended execution and records persistent heartbeats."""

    def __init__(
        self,
        *,
        clock: Clock | None = None,
        schedule: DaemonSchedule | None = None,
        heartbeat_path: Path | None = None,
        on_phase_change: Callable[[DaemonPhase, DaemonPhase], None] | None = None,
        on_tick: Callable[[DaemonPhase], None] | None = None,
    ) -> None:
        self._clock = clock or WallClock()
        self._schedule = schedule or DaemonSchedule()
        self._heartbeat_path = heartbeat_path or Path("data/daemon_heartbeat.json")
        self._on_phase_change = on_phase_change
        self._on_tick = on_tick
        self._current_phase = DaemonPhase.IDLE
        self._running = False
        self._stop_event = threading.Event()

    @property
    def current_phase(self) -> DaemonPhase:
        return self._current_phase

    @property
    def is_running(self) -> bool:
        return self._running

    def record_heartbeat(
        self, phase: DaemonPhase, details: dict[str, Any] | None = None
    ) -> None:
        """Write crash-safe heartbeat metadata to disk."""
        now = self._clock.now_utc()
        payload = {
            "timestamp": now.isoformat(),
            "local_time": now.astimezone(IST).isoformat(),
            "phase": phase.value,
            "details": details or {},
        }
        self._heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self._heartbeat_path.with_suffix(".tmp")
        temp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temp_path.replace(self._heartbeat_path)

    def tick(self) -> DaemonPhase:
        """Run one scheduler evaluation tick."""
        now = self._clock.now_utc()
        next_phase = determine_phase(now, self._schedule)
        if next_phase != self._current_phase:
            logger.info(
                "Daemon phase transition: %s -> %s", self._current_phase, next_phase
            )
            if self._on_phase_change is not None:
                try:
                    self._on_phase_change(self._current_phase, next_phase)
                except Exception:
                    logger.exception("Error in daemon phase transition callback")
            self._current_phase = next_phase

        self.record_heartbeat(self._current_phase)
        if self._on_tick is not None:
            try:
                self._on_tick(self._current_phase)
            except Exception:
                logger.exception("Error in daemon tick callback")
        return self._current_phase

    def stop(self) -> None:
        """Signal the daemon loop to terminate cleanly."""
        self._running = False
        self._stop_event.set()
        self._current_phase = DaemonPhase.STOPPED
        self.record_heartbeat(DaemonPhase.STOPPED)

    def run(self, poll_interval_seconds: float = 10.0) -> None:
        """Run the supervisor loop until stopped or interrupted."""
        self._running = True
        self._stop_event.clear()

        # Handle OS termination signals cleanly where possible
        def _handle_signal(signum: int, _frame: Any) -> None:
            logger.info("Received OS signal %s; shutting down daemon...", signum)
            self.stop()

        try:
            signal.signal(signal.SIGINT, _handle_signal)
            signal.signal(signal.SIGTERM, _handle_signal)
        except (ValueError, AttributeError):
            # Signal handling might not be permitted in non-main threads
            pass

        logger.info("Daemon supervisor started (Asia/Kolkata timetable).")
        while self._running and not self._stop_event.is_set():
            self.tick()
            self._stop_event.wait(timeout=poll_interval_seconds)

        logger.info("Daemon supervisor stopped.")
