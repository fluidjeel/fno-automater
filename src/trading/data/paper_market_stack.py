"""Wired PAPER market-data stack: cached chain, promotion and depth websocket."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from trading.config.depth_promotion import (
    DepthPromotionConfig,
    load_depth_promotion_config,
)
from trading.data.config import DataPipelineConfig
from trading.data.depth_promotion import DepthSetPromoter
from trading.data.fyers.client import FyersMarketFeed
from trading.data.paper_chain_cache import (
    CachedOptionChainFeed,
    ChainCachingMarketFeed,
)
from trading.data.pipeline import DataPipeline, build_pipeline
from trading.data.promoted_depth_ws import PromotedDepthWebSocket
from trading.data.settings import FyersSettings
from trading.domain.clock import Clock

__all__ = ["PaperMarketStack", "build_paper_market_stack"]


@dataclass(slots=True)
class PaperMarketStack:
    """Shared PAPER session market-data components."""

    feed: ChainCachingMarketFeed
    pipeline: DataPipeline
    depth_config: DepthPromotionConfig
    depth_promoter: DepthSetPromoter
    depth_ws: PromotedDepthWebSocket
    last_chain_health: object | None = None
    last_promotion_health: object | None = None
    last_attachment_health: object | None = None


def build_paper_market_stack(
    repo_root: Path,
    *,
    clock: Clock,
    pipeline_cfg: DataPipelineConfig,
    settings: FyersSettings,
) -> PaperMarketStack:
    """Construct cached chain feed, pipeline, promoter and depth websocket."""
    depth_config = load_depth_promotion_config(repo_root / "config" / "paper_data.yaml")
    base_feed = FyersMarketFeed(
        settings,
        clock,
        strike_count=pipeline_cfg.fyers.option_chain_strike_count,
        chain_greeks=pipeline_cfg.fyers.chain_greeks,
        history_oi_flag=pipeline_cfg.fyers.history_oi_flag,
    )
    chain_cache = CachedOptionChainFeed(base_feed, depth_config, clock)
    feed = ChainCachingMarketFeed(base_feed, chain_cache)
    pipeline = build_pipeline(repo_root, feed=feed)
    depth_ws = PromotedDepthWebSocket(
        settings,
        clock,
        repo_root,
        depth_config,
    )
    depth_ws.start()
    return PaperMarketStack(
        feed=feed,
        pipeline=pipeline,
        depth_config=depth_config,
        depth_promoter=DepthSetPromoter(depth_config),
        depth_ws=depth_ws,
    )
