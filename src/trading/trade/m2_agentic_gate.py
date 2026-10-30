"""Map agent direction to gate action (paper free-trade)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

from trading.data.m2_agentic_pipeline.config import M2AgenticPipelineConfig
from trading.trade.m2_veto import M2VetoAction

__all__ = ["AgentDirection", "GateMapping", "alignment_label", "map_m2_agentic_gate"]


AgentDirection = Literal["LONG", "SHORT", "NEUTRAL"]


@dataclass(frozen=True, slots=True)
class GateMapping:
    """Precomputed gate fields written with the verdict."""

    action: M2VetoAction
    size_fraction: Decimal
    alignment: str
    stale_data_veto: bool
    event_blackout_veto: bool


def alignment_label(platter: dict[str, Any], direction: AgentDirection) -> str:
    """Compare agent direction to mechanical setup_direction."""
    mech = str(platter.get("mechanical", {}).get("setup_direction") or "NONE")
    if mech == "NONE":
        return "na"
    if direction == "LONG" and mech == "UP":
        return "aligned"
    if direction == "SHORT" and mech == "DOWN":
        return "aligned"
    if direction == "NEUTRAL":
        return "na"
    return "conflict"


def map_m2_agentic_gate(
    platter: dict[str, Any],
    direction: AgentDirection,
    *,
    config: M2AgenticPipelineConfig,
    missing_decision_packet: bool = False,
) -> GateMapping:
    """Deterministic gate mapping from revamp spec."""
    events = platter.get("events") or {}
    blackout = bool(events.get("blackout_active"))
    stale = bool(platter.get("fail_closed")) or missing_decision_packet
    align = alignment_label(platter, direction)
    if missing_decision_packet or stale:
        return GateMapping(
            action=M2VetoAction.VETO,
            size_fraction=Decimal("0"),
            alignment=align,
            stale_data_veto=stale,
            event_blackout_veto=False,
        )
    if blackout:
        return GateMapping(
            action=M2VetoAction.VETO,
            size_fraction=Decimal("0"),
            alignment=align,
            stale_data_veto=False,
            event_blackout_veto=True,
        )
    if config.paper_mode.free_trade:
        return GateMapping(
            action=M2VetoAction.APPROVE,
            size_fraction=Decimal("1"),
            alignment=align,
            stale_data_veto=False,
            event_blackout_veto=False,
        )
    mech = str(platter.get("mechanical", {}).get("setup_direction") or "NONE")
    if align == "conflict":
        return GateMapping(
            action=M2VetoAction.SIZE_DOWN,
            size_fraction=Decimal("0.5"),
            alignment=align,
            stale_data_veto=False,
            event_blackout_veto=False,
        )
    if direction == "NEUTRAL" and mech != "NONE":
        return GateMapping(
            action=M2VetoAction.SIZE_DOWN,
            size_fraction=Decimal("0.5"),
            alignment=align,
            stale_data_veto=False,
            event_blackout_veto=False,
        )
    return GateMapping(
        action=M2VetoAction.APPROVE,
        size_fraction=Decimal("1"),
        alignment=align,
        stale_data_veto=False,
        event_blackout_veto=False,
    )
