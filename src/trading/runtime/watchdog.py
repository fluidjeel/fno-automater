"""External watchdog for the PAPER protection heartbeat."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from trading.domain.clock import Clock, WallClock
from trading.domain.contracts import AttentionRequest, ProtectionHeartbeat
from trading.domain.enums import AttentionBlocker, ReasonCode
from trading.domain.ids import SequentialIdFactory
from trading.ops.attention import MemoryAttentionSink
from trading.ops.operator_alert import notify_operator
from trading.runtime.paper_session import load_paper_session_config

__all__ = ["run_paper_watchdog"]


def run_paper_watchdog(
    repo_root: Path,
    *,
    store_path: Path | None = None,
    notifier: MemoryAttentionSink | None = None,
    clock: Clock | None = None,
) -> int:
    """Return 0 when healthy, 1 when protection monitoring appears stopped."""
    session_cfg = load_paper_session_config(repo_root / "config" / "paper_session.yaml")
    heartbeat_path = repo_root / session_cfg.protection.heartbeat_path
    if not heartbeat_path.is_file():
        return 1
    payload = json.loads(heartbeat_path.read_text(encoding="utf-8"))
    heartbeat = ProtectionHeartbeat.model_validate(payload)
    wall = clock or WallClock()
    now = wall.now_utc()
    silence = now - heartbeat.as_of
    if heartbeat.open_positions <= 0:
        return 0
    max_silence = session_cfg.protection.watchdog_max_silence_seconds
    if silence <= timedelta(seconds=max_silence):
        return 0
    sink = notifier or MemoryAttentionSink()
    ids = SequentialIdFactory(now)
    request = AttentionRequest(
        request_id=ids.new_id("ATT"),
        blocker=AttentionBlocker.PROTECTION_DEGRADED,
        reason_code=ReasonCode.PROTECTION_DEGRADED,
        detail=(
            f"protection heartbeat silent for {int(silence.total_seconds())}s "
            f"with {heartbeat.open_positions} open position(s)"
        ),
        required_artifact="fresh protection heartbeat from trading paper session",
        as_of_time=now,
        valid_until=now + timedelta(days=1),
    )
    sink.submit(request)
    notify_operator(
        repo_root,
        title="PAPER protection heartbeat stale",
        detail=request.detail,
        dedupe_key="watchdog:protection",
    )
    if store_path is not None and store_path.is_file():
        _ = store_path
    return 1
