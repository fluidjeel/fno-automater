"""Platter assembly package."""

from trading.data.m2_agentic.assemble import (
    assemble_m2_agentic_decision,
    latest_feature_row,
    persist_m2_agentic_decision,
    m2_agentic_decision_path,
)

__all__ = [
    "assemble_m2_agentic_decision",
    "latest_feature_row",
    "persist_m2_agentic_decision",
    "m2_agentic_decision_path",
]
