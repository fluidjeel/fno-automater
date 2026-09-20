"""Paper-only CAS depth collection. Isolated from execution storage."""

from trading.data.cas_depth.adapter import CasDepthPaperAdapter
from trading.data.cas_depth.collector import CasDepthCollector, CollectorRunResult
from trading.data.cas_depth.config import CasDataConfig, load_cas_data_config
from trading.data.cas_depth.contracts import (
    CAS_DEPTH_FEATURE_SET_VERSION,
    CasDepthFeatureSnapshot,
    DepthLevel,
    NormalizedDepthUpdate,
    TradeAggressor,
)

__all__ = [
    "CAS_DEPTH_FEATURE_SET_VERSION",
    "CasDataConfig",
    "CasDepthCollector",
    "CasDepthFeatureSnapshot",
    "CasDepthPaperAdapter",
    "CollectorRunResult",
    "DepthLevel",
    "NormalizedDepthUpdate",
    "TradeAggressor",
    "load_cas_data_config",
]
