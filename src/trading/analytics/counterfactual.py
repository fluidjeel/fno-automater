"""Automated counterfactual P&L evaluation engine.

Measures the exact quantitative alpha added or subtracted by Agent Desk decisions:
1. Vetoes (VETO_ENTRY): Did vetoing entries prevent losses or forfeit gains?
2. Downscaling (REDUCE_SIZE): Did reducing size save capital on losses or reduce gains?
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from trading.domain.enums import AgentAction

__all__ = [
    "CounterfactualEvaluation",
    "CounterfactualTradeInput",
    "evaluate_counterfactuals",
    "format_counterfactual_report",
]


@dataclass(frozen=True, slots=True)
class CounterfactualTradeInput:
    """Decision record joined with hypothetical baseline outcome at full 1.0 size."""

    decision_id: str
    trade_id: str
    action: AgentAction
    size_multiplier: Decimal
    baseline_outcome_r: Decimal  # What the trade returned (in R) at 1.0x size


@dataclass(frozen=True, slots=True)
class CounterfactualEvaluation:
    """Summary of economic value added or destroyed by agent desk decisions."""

    total_evaluated: int
    veto_count: int
    veto_saved_losses_r: Decimal  # Losses successfully prevented
    veto_missed_gains_r: Decimal  # Winners incorrectly blocked
    net_veto_value_r: Decimal  # Saved losses minus missed gains
    downscale_count: int
    downscale_saved_r: Decimal  # Reduced losses on losing trades
    downscale_missed_r: Decimal  # Forfeited profit on winning trades
    net_downscale_value_r: Decimal
    net_agent_alpha_r: Decimal  # Total economic contribution in R

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_evaluated": self.total_evaluated,
            "veto_count": self.veto_count,
            "veto_saved_losses_r": str(self.veto_saved_losses_r),
            "veto_missed_gains_r": str(self.veto_missed_gains_r),
            "net_veto_value_r": str(self.net_veto_value_r),
            "downscale_count": self.downscale_count,
            "downscale_saved_r": str(self.downscale_saved_r),
            "downscale_missed_r": str(self.downscale_missed_r),
            "net_downscale_value_r": str(self.net_downscale_value_r),
            "net_agent_alpha_r": str(self.net_agent_alpha_r),
        }


def evaluate_counterfactuals(
    records: Sequence[CounterfactualTradeInput],
) -> CounterfactualEvaluation:
    """Compute exact counterfactual performance across decisions."""
    veto_count = 0
    veto_saved = Decimal("0")
    veto_missed = Decimal("0")

    downscale_count = 0
    downscale_saved = Decimal("0")
    downscale_missed = Decimal("0")

    for item in records:
        r = item.baseline_outcome_r

        # 1. Handle Vetoes
        if item.action is AgentAction.VETO_ENTRY:
            veto_count += 1
            if r < 0:
                # Avoided loss: positive contribution equal to avoided loss magnitude
                veto_saved += abs(r)
            elif r > 0:
                # Forfeited a gain: negative contribution equal to the missed profit
                veto_missed += r

        # 2. Handle Downscaled Sizing
        elif item.size_multiplier < Decimal("1.0"):
            downscale_count += 1
            reduction = Decimal("1.0") - item.size_multiplier
            if r < 0:
                # Saved loss: reduction fraction times loss magnitude
                downscale_saved += reduction * abs(r)
            elif r > 0:
                # Forfeited gain: reduction fraction times gain magnitude
                downscale_missed += reduction * r

    net_veto = veto_saved - veto_missed
    net_downscale = downscale_saved - downscale_missed
    net_alpha = net_veto + net_downscale

    return CounterfactualEvaluation(
        total_evaluated=len(records),
        veto_count=veto_count,
        veto_saved_losses_r=veto_saved,
        veto_missed_gains_r=veto_missed,
        net_veto_value_r=net_veto,
        downscale_count=downscale_count,
        downscale_saved_r=downscale_saved,
        downscale_missed_r=downscale_missed,
        net_downscale_value_r=net_downscale,
        net_agent_alpha_r=net_alpha,
    )


def format_counterfactual_report(eval_res: CounterfactualEvaluation) -> str:
    """Format markdown report of agent decision counterfactuals."""
    alpha_status = (
        "POSITIVE ALPHA (+)"
        if eval_res.net_agent_alpha_r >= 0
        else "NEGATIVE ALPHA (-)"
    )
    lines = [
        "=== AGENT DESK COUNTERFACTUAL ALPHA AUDIT ===",
        f"Overall Contribution: {alpha_status} ({eval_res.net_agent_alpha_r:+.2f} R)",
        f"Decisions Evaluated: {eval_res.total_evaluated}",
        "",
        f"• Veto Analysis ({eval_res.veto_count} vetoes):",
        f"  - Losses Prevented: +{eval_res.veto_saved_losses_r:.2f} R",
        f"  - Profits Forfeited: -{eval_res.veto_missed_gains_r:.2f} R",
        f"  - Net Veto Value: {eval_res.net_veto_value_r:+.2f} R",
        "",
        f"• Sizing Downscale Analysis ({eval_res.downscale_count} scaled trades):",
        f"  - Capital Saved on Losers: +{eval_res.downscale_saved_r:.2f} R",
        f"  - Profit Missed on Winners: -{eval_res.downscale_missed_r:.2f} R",
        f"  - Net Downscale Value: {eval_res.net_downscale_value_r:+.2f} R",
    ]
    return "\n".join(lines)
