"""Deterministic systemd unit orchestration for unattended PAPER operations."""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

__all__ = [
    "ServiceState",
    "ServiceSupervisor",
    "SystemdServiceSupervisor",
]

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ServiceState:
    """Observed systemd unit state."""

    name: str
    active: bool
    enabled: bool
    detail: str


class ServiceSupervisor(Protocol):
    """Port for starting and observing supervised units."""

    def state(self, unit: str) -> ServiceState:
        """Return the current unit state."""

    def ensure_active(self, unit: str) -> ServiceState:
        """Start the unit when it is not already active."""


class SystemdServiceSupervisor:
    """Invoke systemctl to supervise long-running PAPER services."""

    def __init__(self, *, dry_run: bool = False) -> None:
        self._dry_run = dry_run

    def state(self, unit: str) -> ServiceState:
        show = self._run(
            "show",
            unit,
            "--property=ActiveState,UnitFileState",
            "--value",
        )
        if show.returncode != 0:
            return ServiceState(
                name=unit,
                active=False,
                enabled=False,
                detail=(show.stderr or show.stdout or "unavailable").strip(),
            )
        lines = [line.strip() for line in show.stdout.splitlines() if line.strip()]
        active_state = lines[0] if lines else "unknown"
        enabled_state = lines[1] if len(lines) > 1 else "unknown"
        return ServiceState(
            name=unit,
            active=active_state == "active",
            enabled=enabled_state in {"enabled", "static"},
            detail=f"{active_state}/{enabled_state}",
        )

    def ensure_active(self, unit: str) -> ServiceState:
        current = self.state(unit)
        if current.active:
            return current
        if self._dry_run:
            logger.info("dry-run: would start %s", unit)
            return ServiceState(
                name=unit,
                active=False,
                enabled=current.enabled,
                detail="dry-run",
            )
        start = self._run("start", unit)
        if start.returncode != 0:
            detail = (start.stderr or start.stdout or "start failed").strip()
            logger.error("failed to start %s: %s", unit, detail)
            return ServiceState(
                name=unit,
                active=False,
                enabled=current.enabled,
                detail=detail,
            )
        return self.state(unit)

    def restart(self, unit: str) -> ServiceState:
        if self._dry_run:
            logger.info("dry-run: would restart %s", unit)
            return self.state(unit)
        proc = self._run("restart", unit)
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "restart failed").strip()
            logger.error("failed to restart %s: %s", unit, detail)
        return self.state(unit)

    def ensure_active_many(self, units: Sequence[str]) -> tuple[ServiceState, ...]:
        return tuple(self.ensure_active(unit) for unit in units)

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        command = self._command(args)
        logger.debug("running %s", " ".join(command))
        return subprocess.run(  # noqa: S603 - argv only; no shell execution
            command,
            check=False,
            capture_output=True,
            text=True,
        )

    def _command(self, args: tuple[str, ...]) -> list[str]:
        if args and args[0] in {"start", "restart"}:
            return ["sudo", "-n", "systemctl", *args]
        return ["systemctl", *args]
