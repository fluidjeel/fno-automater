"""Soft rubric scores from a platter dict."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from trading.data.m2_agentic_pipeline.config import M2AgenticPipelineConfig

__all__ = ["RubricResult", "score_m2_agentic_decision"]


class RubricResult:
    """Rubric output before optional LLM overlay."""

    __slots__ = ("confidence", "direction", "pillar_scores", "reasons", "score_total")

    def __init__(
        self,
        *,
        direction: str,
        score_total: Decimal,
        pillar_scores: dict[str, int],
        confidence: Decimal,
        reasons: list[str],
    ) -> None:
        self.direction = direction
        self.score_total = score_total
        self.pillar_scores = pillar_scores
        self.confidence = confidence
        self.reasons = reasons


def _pillar(flow_delta: int) -> int:
    if flow_delta > 500:
        return 2
    if flow_delta > 0:
        return 1
    if flow_delta < -500:
        return -2
    if flow_delta < 0:
        return -1
    return 0


def score_m2_agentic_decision(platter: dict[str, Any], config: M2AgenticPipelineConfig) -> RubricResult:
    """Weighted sum with config cutoffs (soft defaults)."""
    flow = platter.get("flow") or {}
    depth = platter.get("depth") or {}
    weights = config.rubric_weights
    pillars = {
        "structure": 0,
        "flow": _pillar(int(flow.get("session_delta") or 0)),
        "depth": 1 if Decimal(str(depth.get("imbalance_l5") or "0")) > 0 else -1,
        "vol": 0,
        "options": 0,
        "events": -2 if (platter.get("events") or {}).get("blackout_active") else 0,
    }
    total = Decimal("0")
    for name, score in pillars.items():
        total += Decimal(score) * Decimal(getattr(weights, name))
    cutoff = Decimal(config.rubric_direction_cutoff)
    if total >= cutoff:
        direction = "LONG"
    elif total <= -cutoff:
        direction = "SHORT"
    else:
        direction = "NEUTRAL"
    reasons = [f"rubric_total={total}"]
    confidence = min(Decimal("1"), abs(total) / cutoff) if cutoff > 0 else Decimal("0")
    return RubricResult(
        direction=direction,
        score_total=total,
        pillar_scores=pillars,
        confidence=confidence,
        reasons=reasons,
    )
