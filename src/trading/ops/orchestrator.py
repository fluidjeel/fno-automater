"""Phase-driven service orchestration for unattended PAPER autopilot."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from trading.data.fyers.auth import run_telegram_auth
from trading.ops.daemon import DaemonPhase
from trading.ops.operator_alert import notify_operator
from trading.ops.service_supervisor import (
    ServiceState,
    ServiceSupervisor,
    SystemdServiceSupervisor,
)
from trading.runtime.watchdog import run_paper_watchdog

__all__ = ["PAPER_RUNTIME_UNITS", "PaperAutopilotOrchestrator"]

logger = logging.getLogger(__name__)

PAPER_RUNTIME_UNITS: tuple[str, ...] = (
    "fno-data-tick.service",
    "fno-paper-session.service",
)


@dataclass(frozen=True, slots=True)
class OrchestratorResult:
    """One autopilot evaluation."""

    phase: DaemonPhase
    services: tuple[object, ...]
    auth_status: int | None
    watchdog_status: int | None
    restarted_paper: bool


class PaperAutopilotOrchestrator:
    """Ensure PAPER services run through the IST session without operator action."""

    def __init__(
        self,
        repo_root: Path,
        *,
        supervisor: ServiceSupervisor | None = None,
        dry_run: bool = False,
        skip_auth: bool = False,
    ) -> None:
        self._repo_root = repo_root
        self._supervisor = supervisor or SystemdServiceSupervisor(dry_run=dry_run)
        self._dry_run = dry_run
        self._skip_auth = skip_auth

    def on_phase(
        self, phase: DaemonPhase, *, previous: DaemonPhase
    ) -> OrchestratorResult:
        """React to a daemon phase transition."""
        auth_status: int | None = None
        if (
            not self._skip_auth
            and previous is not phase
            and phase is DaemonPhase.PRE_MARKET
        ):
            auth_status = self._ensure_market_auth()
            if auth_status != 0:
                self._alert_auth_failure(auth_status)
        services = self._ensure_runtime_services(phase)
        self._alert_service_failures(services)
        watchdog_status, restarted = self._maybe_recover_paper(phase)
        return OrchestratorResult(
            phase=phase,
            services=services,
            auth_status=auth_status,
            watchdog_status=watchdog_status,
            restarted_paper=restarted,
        )

    def tick(self, phase: DaemonPhase) -> OrchestratorResult:
        """Periodic health pass while the supervisor loop is running."""
        services = self._ensure_runtime_services(phase)
        self._alert_service_failures(services)
        watchdog_status, restarted = self._maybe_recover_paper(phase)
        return OrchestratorResult(
            phase=phase,
            services=services,
            auth_status=None,
            watchdog_status=watchdog_status,
            restarted_paper=restarted,
        )

    def _ensure_runtime_services(self, phase: DaemonPhase) -> tuple[object, ...]:
        if phase not in {
            DaemonPhase.PRE_MARKET,
            DaemonPhase.MARKET_ACTIVE,
            DaemonPhase.POST_MARKET,
        }:
            return ()
        if isinstance(self._supervisor, SystemdServiceSupervisor):
            return self._supervisor.ensure_active_many(PAPER_RUNTIME_UNITS)
        return tuple(
            self._supervisor.ensure_active(unit) for unit in PAPER_RUNTIME_UNITS
        )

    def _ensure_market_auth(self) -> int:
        if self._dry_run:
            logger.info("dry-run: would verify Fyers auth via Telegram")
            return 0
        status = run_telegram_auth(self._repo_root)
        if status != 0:
            logger.error("Fyers Telegram auth failed with status %s", status)
        return status

    def _maybe_recover_paper(self, phase: DaemonPhase) -> tuple[int | None, bool]:
        if phase is not DaemonPhase.MARKET_ACTIVE:
            return None, False
        if self._dry_run:
            return 0, False
        status = run_paper_watchdog(self._repo_root)
        if status == 0:
            return status, False
        notify_operator(
            self._repo_root,
            title="PAPER protection watchdog unhealthy",
            detail="Restarting fno-paper-session.service",
            dedupe_key="watchdog:protection",
        )
        logger.warning("paper watchdog unhealthy; restarting fno-paper-session.service")
        paper_unit = "fno-paper-session.service"
        restart = getattr(self._supervisor, "restart", None)
        if restart is not None:
            restarted = restart(paper_unit)
            if isinstance(restarted, ServiceState) and not restarted.active:
                notify_operator(
                    self._repo_root,
                    title=f"{paper_unit} restart failed",
                    detail=restarted.detail,
                    dedupe_key=f"restart:{paper_unit}",
                )
        return status, True

    def _alert_auth_failure(self, status: int) -> None:
        notify_operator(
            self._repo_root,
            title="Fyers Telegram auth failed",
            detail=(
                f"run_telegram_auth exited {status}. "
                "Paper session may not receive market data until auth succeeds."
            ),
            dedupe_key="auth:telegram",
        )

    def _alert_service_failures(self, services: tuple[object, ...]) -> None:
        for service in services:
            if not isinstance(service, ServiceState):
                continue
            if service.active:
                continue
            notify_operator(
                self._repo_root,
                title=f"{service.name} not running",
                detail=(
                    f"Autopilot could not keep {service.name} active: {service.detail}"
                ),
                dedupe_key=f"service:{service.name}",
            )
