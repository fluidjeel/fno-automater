"""Minute backbone then features in one deterministic step."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from trading.data.features.compute import compute_feature_row, persist_feature_row
from trading.data.features.delta_state import load_delta_state
from trading.data.minute_backbone.models import MinuteBundle
from trading.data.minute_backbone.scheduler import run_minute_backbone_once
from trading.data.m2_agentic_pipeline.config import load_m2_agentic_pipeline_config
from trading.domain.clock import Clock

_IST = ZoneInfo("Asia/Kolkata")

__all__ = ["run_minute_and_features"]


def run_minute_and_features(repo_root: Path, *, clock: Clock) -> tuple[Path, Path] | None:
    """Run REST minute snapshot then feature row for the same minute."""
    minute_path = run_minute_backbone_once(repo_root, clock=clock)
    if minute_path is None:
        return None
    bundle = MinuteBundle.model_validate(
        json.loads(minute_path.read_text(encoding="utf-8"))
    )
    config = load_m2_agentic_pipeline_config(repo_root / "config" / "m2_agentic_pipeline.yaml")
    session_date = bundle.minute_start_ist.astimezone(_IST).date().isoformat()
    delta = load_delta_state(repo_root, session_date)
    if delta is None:
        from trading.data.tick_consumer.models import DeltaState
        from datetime import date as date_cls

        delta = DeltaState(session_date=date_cls.fromisoformat(session_date))
    row = compute_feature_row(
        repo_root,
        bundle,
        config=config,
        delta=delta,
    )
    feature_path = persist_feature_row(repo_root, row)
    return minute_path, feature_path
