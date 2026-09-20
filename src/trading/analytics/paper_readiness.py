"""Paper trading readiness and promotion evaluation engine (PAPER-005).

Evaluates whether forward paper trading evidence has accumulated sufficient
sample size (N >= 150), calibrated decision scores, verified LIVE market rules,
and zero safety violations to unlock the minimal-capital CANARY promotion gate.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from trading.domain.clock import WallClock

__all__ = [
    "DEFAULT_TARGET_DECISIONS",
    "PaperReadinessReport",
    "evaluate_paper_readiness",
    "format_paper_readiness",
]

DEFAULT_TARGET_DECISIONS = 150


@dataclass(frozen=True, slots=True)
class PaperReadinessReport:
    """Audit report for paper execution promotion eligibility."""

    as_of: datetime
    total_decisions: int
    target_decisions: int
    sample_sufficient: bool
    charges_verified: bool
    live_config_verified: bool
    safety_violations: int
    brier_score: Decimal | None
    max_brier_threshold: Decimal
    is_promotion_eligible: bool
    blockers: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of.isoformat(),
            "total_decisions": self.total_decisions,
            "target_decisions": self.target_decisions,
            "sample_sufficient": self.sample_sufficient,
            "charges_verified": self.charges_verified,
            "live_config_verified": self.live_config_verified,
            "safety_violations": self.safety_violations,
            "brier_score": str(self.brier_score)
            if self.brier_score is not None
            else None,
            "is_promotion_eligible": self.is_promotion_eligible,
            "blockers": list(self.blockers),
        }


def evaluate_paper_readiness(
    *,
    total_decisions: int,
    charges_verified: bool,
    live_config_verified: bool,
    safety_violations: int = 0,
    brier_score: Decimal | None = None,
    target_decisions: int = DEFAULT_TARGET_DECISIONS,
    max_brier_threshold: Decimal = Decimal("0.25"),
    as_of: datetime | None = None,
) -> PaperReadinessReport:
    """Audit paper evidence against strict promotion criteria."""
    now = as_of or WallClock().now_utc()
    blockers: list[str] = []

    sample_sufficient = total_decisions >= target_decisions
    if not sample_sufficient:
        blockers.append(
            f"INSUFFICIENT_SAMPLE: {total_decisions}/{target_decisions} decisions"
        )

    if not charges_verified:
        blockers.append("CHARGES_UNVERIFIED: broker & statutory charges unverified")

    if not live_config_verified:
        blockers.append("LIVE_CONFIG_UNVERIFIED: live market rules unverified")

    if safety_violations > 0:
        blockers.append(
            f"SAFETY_VIOLATIONS: {safety_violations} invariant violations detected"
        )

    if brier_score is not None and brier_score > max_brier_threshold:
        blockers.append(
            f"BRIER_POOR: score {brier_score} exceeds threshold {max_brier_threshold}"
        )

    is_eligible = len(blockers) == 0

    return PaperReadinessReport(
        as_of=now,
        total_decisions=total_decisions,
        target_decisions=target_decisions,
        sample_sufficient=sample_sufficient,
        charges_verified=charges_verified,
        live_config_verified=live_config_verified,
        safety_violations=safety_violations,
        brier_score=brier_score,
        max_brier_threshold=max_brier_threshold,
        is_promotion_eligible=is_eligible,
        blockers=tuple(blockers),
    )


def format_paper_readiness(report: PaperReadinessReport) -> str:
    """Format human-readable markdown summary for CLI / attention alerting."""
    status = (
        "✅ ELIGIBLE FOR CANARY"
        if report.is_promotion_eligible
        else "❌ PROMOTION BLOCKED"
    )
    brier_display = str(report.brier_score) if report.brier_score is not None else "N/A"
    lines = [
        "=== PAPER PROMOTION READINESS AUDIT (PAPER-005) ===",
        f"Status: {status}",
        f"Decisions Accumulated: {report.total_decisions} / {report.target_decisions}",
        f"Charges Verified: {'YES' if report.charges_verified else 'NO'}",
        f"Live Config Verified: {'YES' if report.live_config_verified else 'NO'}",
        f"Safety Violations: {report.safety_violations}",
        f"Brier Score: {brier_display}",
    ]
    if report.blockers:
        lines.append("Active Blockers:")
        for b in report.blockers:
            lines.append(f"  • {b}")
    return "\n".join(lines)
