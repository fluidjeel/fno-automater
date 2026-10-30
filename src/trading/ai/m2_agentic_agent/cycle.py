"""Five-minute M2 agentic agent cycle."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from trading.ai.m2_agentic_agent.llm_overlay import decide_agent_direction, resolve_llm_enabled
from trading.analytics.m2_agentic_telemetry import append_decision_record
from trading.data.m2_agentic.assemble import (
    assemble_m2_agentic_decision,
    latest_feature_row,
    load_minute_bundle_for_mark,
    persist_m2_agentic_decision,
)
from trading.data.m2_agentic_pipeline.config import load_m2_agentic_pipeline_config
from trading.domain.clock import Clock
from trading.trade.m2_veto import M2VetoVerdict, load_m2_veto_config, write_verdict
from trading.trade.m2_agentic_gate import map_m2_agentic_gate

__all__ = ["run_m2_agentic_agent_cycle"]


def run_m2_agentic_agent_cycle(
    repo_root: Path,
    *,
    clock: Clock,
    m2_agentic_mark_ist: datetime | None = None,
    llm_enabled: bool | None = None,
    llm_client: Any | None = None,
) -> Path | None:
    """Build platter if needed, score agent, write verdict and telemetry."""
    config = load_m2_agentic_pipeline_config(repo_root / "config" / "m2_agentic_pipeline.yaml")
    use_llm = resolve_llm_enabled(config, cli_override=llm_enabled)
    veto_config = load_m2_veto_config(repo_root / "config" / "m2_veto.yaml")
    now = clock.now_utc()
    mark = m2_agentic_mark_ist or now
    loaded = latest_feature_row(repo_root, mark)
    if loaded is None:
        gate = map_m2_agentic_gate({}, "NEUTRAL", config=config, missing_decision_packet=True)
        decision_id = str(uuid.uuid4())
        verdict = M2VetoVerdict(
            action=gate.action,
            size_fraction=gate.size_fraction,
            reasons=("missing platter feature row",),
            as_of=now,
            valid_until=now + timedelta(seconds=veto_config.valid_for_seconds),
            source="m2-agentic-agent-v1",
            decision_id=decision_id,
        )
        write_verdict(verdict, veto_config, repo_root=repo_root)
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
    platter = assemble_m2_agentic_decision(
        repo_root,
        m2_agentic_mark_ist=mark,
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
    m2_agentic_decision_path = persist_m2_agentic_decision(repo_root, platter)
    decision = decide_agent_direction(
        platter,
        config,
        llm_enabled=use_llm,
        llm_client=llm_client,
        repo_root=repo_root,
    )
    direction = decision.direction
    gate = map_m2_agentic_gate(platter, direction, config=config)
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
        m2_agentic_snapshot_path=str(m2_agentic_decision_path.relative_to(repo_root)),
        agent_output=agent_output,
        gate=gate,
        decision_id=decision_id,
    )
    verdict = M2VetoVerdict(
        action=gate.action,
        size_fraction=gate.size_fraction,
        reasons=tuple(decision.reasons[:3]) or ("M2 agentic agent",),
        as_of=now,
        valid_until=now + timedelta(seconds=veto_config.valid_for_seconds),
        source="m2-agentic-agent-v1",
        direction=direction,
        m2_agentic_id=str(platter["m2_agentic_id"]),
        decision_id=decision_id,
    )
    write_verdict(verdict, veto_config, repo_root=repo_root)
    return m2_agentic_decision_path
