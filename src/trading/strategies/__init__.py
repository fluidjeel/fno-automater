"""Layer 3: deterministic strategy decision systems.

Strategies consume an immutable ``FeatureSnapshot`` and a read-only
``PortfolioView`` and emit zero or more ``TradeIntent`` objects. They never
name a quantity, call a broker, reserve capital, or consult an LLM.
"""

from __future__ import annotations

from trading.strategies.base import (
    Rejection,
    Strategy,
    StrategyContext,
    StrategyDecision,
    build_strategy,
)
from trading.strategies.cas_microstructure import CasMicrostructureStrategy
from trading.strategies.commodity_futures import CommodityFuturesStrategy
from trading.strategies.debit_spread import DebitSpreadStrategy
from trading.strategies.long_option import LongOptionStrategy
from trading.strategies.macro import MacroAssessment, MacroBias
from trading.strategies.multileg_options import MultiLegOptionsStrategy

__all__ = [
    "CasMicrostructureStrategy",
    "CommodityFuturesStrategy",
    "DebitSpreadStrategy",
    "LongOptionStrategy",
    "MacroAssessment",
    "MacroBias",
    "MultiLegOptionsStrategy",
    "Rejection",
    "Strategy",
    "StrategyContext",
    "StrategyDecision",
    "build_strategy",
]
