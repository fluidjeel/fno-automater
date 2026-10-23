"""Layer 3: deterministic strategy decision systems (M2 branch — long option focus)."""

from __future__ import annotations

from trading.strategies.base import (
    Rejection,
    Strategy,
    StrategyContext,
    StrategyDecision,
    build_strategy,
)
from trading.strategies.commodity_futures import CommodityFuturesStrategy
from trading.strategies.long_option import LongOptionStrategy
from trading.strategies.macro import MacroAssessment, MacroBias

__all__ = [
    "CommodityFuturesStrategy",
    "LongOptionStrategy",
    "MacroAssessment",
    "MacroBias",
    "Rejection",
    "Strategy",
    "StrategyContext",
    "StrategyDecision",
    "build_strategy",
]
