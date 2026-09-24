"""Autopilot orchestration: phase-driven service supervision."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from trading.ops.daemon import DaemonPhase
from trading.ops.orchestrator import PAPER_RUNTIME_UNITS, PaperAutopilotOrchestrator
from trading.ops.service_supervisor import ServiceState

ROOT = Path(__file__).resolve().parents[1]


@dataclass
class _FakeSupervisor:
    started: list[str]
    restarted: list[str]

    def state(self, unit: str) -> ServiceState:
        return ServiceState(
            name=unit,
            active=unit in self.started,
            enabled=True,
            detail="active/enabled",
        )

    def ensure_active(self, unit: str) -> ServiceState:
        if unit not in self.started:
            self.started.append(unit)
        return self.state(unit)

    def restart(self, unit: str) -> ServiceState:
        self.restarted.append(unit)
        if unit not in self.started:
            self.started.append(unit)
        return self.state(unit)


def test_pre_market_starts_runtime_units(tmp_path: Path) -> None:
    supervisor = _FakeSupervisor(started=[], restarted=[])
    orchestrator = PaperAutopilotOrchestrator(
        tmp_path,
        supervisor=supervisor,
        skip_auth=True,
    )
    result = orchestrator.on_phase(
        DaemonPhase.PRE_MARKET,
        previous=DaemonPhase.IDLE,
    )
    assert supervisor.started == list(PAPER_RUNTIME_UNITS)
    assert result.auth_status is None


def test_market_active_ensures_services_on_tick() -> None:
    supervisor = _FakeSupervisor(started=[], restarted=[])
    orchestrator = PaperAutopilotOrchestrator(
        ROOT,
        supervisor=supervisor,
        skip_auth=True,
        dry_run=True,
    )
    orchestrator.tick(DaemonPhase.MARKET_ACTIVE)
    assert supervisor.started == list(PAPER_RUNTIME_UNITS)


def test_idle_does_not_start_services(tmp_path: Path) -> None:
    supervisor = _FakeSupervisor(started=[], restarted=[])
    orchestrator = PaperAutopilotOrchestrator(
        tmp_path,
        supervisor=supervisor,
        skip_auth=True,
    )
    orchestrator.tick(DaemonPhase.IDLE)
    assert supervisor.started == []
