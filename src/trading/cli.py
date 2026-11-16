"""CLI for the agentic market-data and five-minute decision stack."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def _repo_root() -> Path:
    env = os.environ.get("TRADING_REPO_ROOT")
    if env:
        return Path(env).resolve()
    return Path(__file__).resolve().parents[2]


def _resolve_repo_path(relative: str) -> Path:
    """Resolve a config path relative to the repo root."""
    path = Path(relative)
    if path.is_absolute():
        return path
    return _repo_root() / path


def _cmd_auth_fyers(args: argparse.Namespace) -> int:
    from trading.data.fyers.auth import run_interactive_auth

    return run_interactive_auth(
        _repo_root(),
        auth_code=args.auth_code,
        open_browser=not args.no_browser,
    )


def _cmd_auth_refresh(_args: argparse.Namespace) -> int:
    from trading.data.fyers.auth import run_refresh

    return run_refresh(_repo_root())


def _cmd_auth_telegram(_args: argparse.Namespace) -> int:
    from trading.data.fyers.auth import run_telegram_auth

    return run_telegram_auth(_repo_root())


def _cmd_m2_agentic_minute(_args: argparse.Namespace) -> int:
    from trading.data.m2_agentic_pipeline.orchestrate import run_minute_and_features
    from trading.domain.clock import WallClock

    root = _repo_root()
    result = run_minute_and_features(root, clock=WallClock())
    if result is None:
        print("minute step failed")
        return 1
    minute_path, feature_path = result
    print(f"minute snapshot: {minute_path}")
    print(f"feature row: {feature_path}")
    return 0


def _cmd_m2_agentic_features(_args: argparse.Namespace) -> int:
    from trading.data.features.compute import compute_feature_row, persist_feature_row
    from trading.data.minute_backbone.models import MinuteBundle
    from trading.data.m2_agentic_pipeline.config import load_m2_agentic_pipeline_config

    root = _repo_root()
    minute_root = root / "data" / "m2_agentic" / "minute"
    if not minute_root.is_dir():
        print("no minute snapshots yet")
        return 1
    latest = max(minute_root.rglob("minute_*.json"), key=lambda p: p.stat().st_mtime)
    bundle = MinuteBundle.model_validate(json.loads(latest.read_text(encoding="utf-8")))
    config = load_m2_agentic_pipeline_config(root / "config" / "m2_agentic_pipeline.yaml")
    row = compute_feature_row(root, bundle, config=config)
    path = persist_feature_row(root, row)
    print(f"feature row: {path}")
    return 0


def _cmd_m2_agentic_agent(args: argparse.Namespace) -> int:
    from trading.ai.m2_agentic_agent.cycle import run_m2_agentic_agent_cycle
    from trading.domain.clock import WallClock

    root = _repo_root()
    llm_override: bool | None = None
    if getattr(args, "no_llm", False):
        llm_override = False
    elif getattr(args, "use_llm", False):
        llm_override = True
    path = run_m2_agentic_agent_cycle(
        root,
        clock=WallClock(),
        llm_enabled=llm_override,
    )
    if path is None:
        print("agent step: no decision packet written")
        return 1
    print(f"decision packet: {path}")
    return 0


def _cmd_m2_agentic_tick_daemon(_args: argparse.Namespace) -> int:
    import threading

    from trading.data.tick_consumer.ws_multiplex import run_tick_multiplex_daemon

    stop = threading.Event()
    try:
        run_tick_multiplex_daemon(_repo_root(), symbol="NSE:NIFTY50-INDEX", stop=stop)
    except KeyboardInterrupt:
        stop.set()
    return 0


def _cmd_m2_agentic_telemetry_retention(_args: argparse.Namespace) -> int:
    from trading.analytics.m2_agentic_telemetry import run_retention_job

    freed = run_retention_job(_repo_root())
    print(f"trade log cleanup freed about {freed} bytes")
    return 0


def _cmd_m2_agentic_telemetry_weekly(_args: argparse.Namespace) -> int:
    from trading.analytics.m2_agentic_telemetry import run_weekly_rollup

    path = run_weekly_rollup(_repo_root())
    print(f"weekly summary: {path}")
    return 0


def _cmd_m2_agentic_shadow_backtest(args: argparse.Namespace) -> int:
    from trading.analytics.m2_agentic_shadow.backtest import run_rubric_backtest

    out = Path(args.output)
    count = run_rubric_backtest(_repo_root(), output_csv=out)
    print(f"shadow replay: {count} marks -> {out}")
    return 0


def _cmd_paper_session(args: argparse.Namespace) -> int:
    from trading.runtime.paper_session import run_paper_session

    return run_paper_session(
        _repo_root(),
        account_config_path=_resolve_repo_path(args.config),
        skip_auth=bool(args.skip_auth),
        once=bool(args.once),
    )


def _cmd_paper_watchdog(_args: argparse.Namespace) -> int:
    from trading.runtime.paper_session import load_paper_session_config
    from trading.runtime.watchdog import run_paper_watchdog

    repo = _repo_root()
    session_cfg = load_paper_session_config(repo / "config" / "paper_session.yaml")
    return run_paper_watchdog(
        repo,
        store_path=repo / session_cfg.store_path,
    )


def _cmd_ops_daemon(args: argparse.Namespace) -> int:
    from trading.ops.daemon import DaemonPhase, DaemonSupervisor
    from trading.ops.orchestrator import PaperAutopilotOrchestrator

    repo = _repo_root()
    orchestrator = PaperAutopilotOrchestrator(
        repo,
        dry_run=bool(args.dry_run),
    )

    def _on_phase_change(previous: DaemonPhase, phase: DaemonPhase) -> None:
        orchestrator.on_phase(phase, previous=previous)

    def _on_tick(phase: DaemonPhase) -> None:
        orchestrator.tick(phase)

    supervisor = DaemonSupervisor(
        heartbeat_path=repo / "data" / "daemon_heartbeat.json",
        on_phase_change=_on_phase_change,
        on_tick=_on_tick,
    )
    print(f"Starting DaemonSupervisor (interval={args.interval}s)")
    if int(args.iterations) > 0:
        for _ in range(int(args.iterations)):
            supervisor.tick()
        return 0
    supervisor.run(poll_interval_seconds=float(args.interval))
    return 0


def _cmd_ops_ensure_paper(_args: argparse.Namespace) -> int:
    from trading.domain.clock import WallClock
    from trading.ops.daemon import determine_phase
    from trading.ops.orchestrator import PaperAutopilotOrchestrator

    repo = _repo_root()
    orchestrator = PaperAutopilotOrchestrator(repo)
    phase = determine_phase(WallClock().now_utc())
    result = orchestrator.tick(phase)
    inactive = [item for item in result.services if not getattr(item, "active", True)]
    return 1 if inactive else 0


def _cmd_ops_alert_unit_failure(args: argparse.Namespace) -> int:
    from trading.ops.operator_alert import notify_operator

    unit = str(args.unit)
    notify_operator(
        _repo_root(),
        title=f"{unit} failed",
        detail=f"systemd OnFailure fired for {unit}.",
        dedupe_key=f"onfailure:{unit}",
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="trading")
    sub = parser.add_subparsers(dest="command", required=True)

    auth = sub.add_parser("auth", help="Fyers login and token refresh")
    auth_sub = auth.add_subparsers(dest="auth_cmd", required=True)
    auth_fyers = auth_sub.add_parser("fyers", help="interactive browser login once")
    auth_fyers.add_argument("--auth-code", default="")
    auth_fyers.add_argument("--no-browser", action="store_true")
    auth_fyers.set_defaults(func=_cmd_auth_fyers)
    auth_refresh = auth_sub.add_parser("refresh", help="silent refresh from saved token")
    auth_refresh.set_defaults(func=_cmd_auth_refresh)
    auth_telegram = auth_sub.add_parser("telegram", help="login via Telegram paste-back")
    auth_telegram.set_defaults(func=_cmd_auth_telegram)

    platter = sub.add_parser("m2-agentic", help="live data pipeline and five-minute agent")
    m2_agentic_sub = platter.add_subparsers(dest="m2_agentic_cmd", required=True)
    m2_agentic_sub.add_parser("minute", help="REST minute snapshot + features").set_defaults(
        func=_cmd_m2_agentic_minute
    )
    m2_agentic_sub.add_parser("features", help="features from latest minute").set_defaults(
        func=_cmd_m2_agentic_features
    )
    agent = m2_agentic_sub.add_parser("agent", help="agent + approval file + trade log line")
    agent.add_argument("--no-llm", action="store_true")
    agent.add_argument("--llm", action="store_true", dest="use_llm")
    agent.set_defaults(func=_cmd_m2_agentic_agent)
    m2_agentic_sub.add_parser("tick-daemon", help="websocket tick writer").set_defaults(
        func=_cmd_m2_agentic_tick_daemon
    )
    tele = m2_agentic_sub.add_parser("telemetry", help="decision log maintenance")
    tele_sub = tele.add_subparsers(dest="tele_cmd", required=True)
    tele_sub.add_parser("retention", help="drop old log partitions").set_defaults(
        func=_cmd_m2_agentic_telemetry_retention
    )
    tele_sub.add_parser("weekly", help="weekly rollup JSON").set_defaults(
        func=_cmd_m2_agentic_telemetry_weekly
    )
    shadow = m2_agentic_sub.add_parser("shadow-backtest", help="offline rubric CSV")
    shadow.add_argument("--output", default="data/telemetry/shadow_backtest.csv")
    shadow.set_defaults(func=_cmd_m2_agentic_shadow_backtest)

    paper = sub.add_parser("paper", help="supervised simulated (PAPER) session")
    paper_sub = paper.add_subparsers(dest="paper_cmd", required=True)
    session = paper_sub.add_parser("session", help="run PAPER trading loop")
    session.add_argument("--config", default="config/paper.yaml")
    session.add_argument("--skip-auth", action="store_true")
    session.add_argument("--once", action="store_true")
    session.set_defaults(func=_cmd_paper_session)
    paper_sub.add_parser("watchdog", help="protection heartbeat check").set_defaults(
        func=_cmd_paper_watchdog
    )

    ops = sub.add_parser("ops", help="unattended PAPER supervisor helpers")
    ops_sub = ops.add_subparsers(dest="ops_cmd", required=True)
    daemon = ops_sub.add_parser("daemon", help="IST session supervisor")
    daemon.add_argument("--interval", type=int, default=60)
    daemon.add_argument("--iterations", type=int, default=0)
    daemon.add_argument("--dry-run", action="store_true")
    daemon.set_defaults(func=_cmd_ops_daemon)
    ops_sub.add_parser(
        "ensure-paper", help="start tick + paper session for current phase"
    ).set_defaults(func=_cmd_ops_ensure_paper)
    alert_failure = ops_sub.add_parser(
        "alert-unit-failure", help="Telegram alert for systemd OnFailure"
    )
    alert_failure.add_argument("unit")
    alert_failure.set_defaults(func=_cmd_ops_alert_unit_failure)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
