"""Five-minute platter agent cycle."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from trading.ai.platter_agent.llm_overlay import decide_agent_direction, resolve_llm_enabled
from trading.analytics.platter_telemetry import append_decision_record
from trading.data.platter.assemble import (
    assemble_platter,
    latest_feature_row,
    load_minute_bundle_for_mark,
    persist_platter,
)
from trading.data.platter_pipeline.config import load_platter_pipeline_config
from trading.domain.clock import Clock
from trading.trade.m2_veto import M2VetoVerdict, load_m2_veto_config, write_verdict
from trading.trade.platter_gate import map_platter_gate

__all__ = ["run_platter_agent_cycle"]


def run_platter_agent_cycle(
    repo_root: Path,
    *,
    clock: Clock,
    platter_mark_ist: datetime | None = None,
    llm_enabled: bool | None = None,
    llm_client: Any | None = None,
) -> Path | None:
    """Build platter if needed, score agent, write verdict and telemetry."""
    config = load_platter_pipeline_config(repo_root / "config" / "platter_pipeline.yaml")
    use_llm = resolve_llm_enabled(config, cli_override=llm_enabled)
    veto_config = load_m2_veto_config()
    now = clock.now_utc()
    mark = platter_mark_ist or now
    loaded = latest_feature_row(repo_root, mark)
    if loaded is None:
        gate = map_platter_gate({}, "NEUTRAL", config=config, missing_platter=True)
        decision_id = str(uuid.uuid4())
        verdict = M2VetoVerdict(
            action=gate.action,
            size_fraction=gate.size_fraction,
            reasons=("missing platter feature row",),
            as_of=now,
            valid_until=now + timedelta(seconds=veto_config.valid_for_seconds),
            source="platter-agent-v1",
            decision_id=decision_id,
        )
        write_verdict(verdict, veto_config)
        return None
    feature_row, feature_path = loaded
    bundle_loaded = load_minute_bundle_for_mark(repo_root, mark)
    minute_bundle = None
    chain_snapshot_id = "unknown"
    depth_snapshot_id = "unknown"
    minute_bundle_path = "unknown"
    nifty_quote: dict[str, Any] = {
        "ltp": "0",
        "bid": "0",
        "ask": "0",
        "as_of_utc": now.isoformat(),
        "source": "rest",
    }
    if bundle_loaded is not None:
        minute_bundle, minute_path_obj = bundle_loaded
        minute_bundle_path = str(minute_path_obj.relative_to(repo_root))
        nifty_quote = dict(minute_bundle.nifty_quote)
        chain_snapshot_id = minute_bundle.chain_snapshot_id
        depth_snapshot_id = minute_bundle.depth_snapshot_id
    platter = assemble_platter(
        repo_root,
        platter_mark_ist=mark,
        feature_row=feature_row,
        minute_bundle_path=minute_bundle_path,
        feature_row_path_value=str(feature_path.relative_to(repo_root)),
        chain_snapshot_id=chain_snapshot_id,
        depth_snapshot_id=depth_snapshot_id,
        nifty_quote=nifty_quote,
        config=config,
        now=now,
        minute_bundle=minute_bundle,
    )
    platter_path = persist_platter(repo_root, platter)
    decision = decide_agent_direction(
        platter,
        config,
        llm_enabled=use_llm,
        llm_client=llm_client,
        repo_root=repo_root,
    )
    direction = decision.direction
    gate = map_platter_gate(platter, direction, config=config)
    decision_id = str(uuid.uuid4())
    agent_output = {
        "direction": direction,
        "score_total": str(decision.rubric.score_total),
        "pillar_scores": decision.rubric.pillar_scores,
        "confidence": str(decision.rubric.confidence),
        "reasons": list(decision.reasons),
        "llm_used": decision.llm_used,
        "llm_failure_reason": decision.llm_failure_reason,
        "model_id": config.agent.model_id,
    }
    append_decision_record(
        repo_root,
        platter=platter,
        platter_snapshot_path=str(platter_path.relative_to(repo_root)),
        agent_output=agent_output,
        gate=gate,
        decision_id=decision_id,
    )
    verdict = M2VetoVerdict(
        action=gate.action,
        size_fraction=gate.size_fraction,
        reasons=tuple(decision.reasons[:3]) or ("platter agent",),
        as_of=now,
        valid_until=now + timedelta(seconds=veto_config.valid_for_seconds),
        source="platter-agent-v1",
        direction=direction,
        platter_id=str(platter["platter_id"]),
        decision_id=decision_id,
    )
    write_verdict(verdict, veto_config)
    return platter_path
