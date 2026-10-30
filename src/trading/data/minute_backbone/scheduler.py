"""Run one minute backbone cycle (chain + depth + merge)."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from trading.data.config import load_data_pipeline_config
from trading.data.fyers.client import FyersApiError, fyers_http_status
from trading.data.fyers.client import FyersMarketFeed
from trading.data.minute_backbone.chain_snapshot import build_chain_snapshot
from trading.data.minute_backbone.depth_snapshot import build_depth_snapshot
from trading.data.minute_backbone.merge import merge_minute_bundle, persist_minute_bundle
from trading.data.minute_backbone.models import ChainSnapshot, DepthSnapshot, MinuteBundle, MinuteStatus
from trading.data.platter_pipeline.config import (
    PlatterPipelineConfig,
    load_platter_pipeline_config,
)
from trading.data.platter_pipeline.depth_hint import publish_depth_hint
from trading.data.settings import FyersSettings
from trading.domain.clock import Clock

_IST = ZoneInfo("Asia/Kolkata")

__all__ = ["run_minute_backbone_once"]


def _quote_dict(capture, symbol: str, *, source: str) -> dict[str, object]:
    data = capture.payload.get("data", capture.payload)
    rows = data if isinstance(data, list) else data.get("d") if isinstance(data, dict) else []
    if not isinstance(rows, list):
        rows = []
    for row in rows:
        if isinstance(row, dict) and row.get("n") == symbol:
            return {
                "ltp": str(row.get("v", {}).get("lp") or row.get("ltp") or "0"),
                "bid": str(row.get("v", {}).get("bid") or row.get("bid") or "0"),
                "ask": str(row.get("v", {}).get("ask") or row.get("ask") or "0"),
                "as_of_utc": capture.received_at.isoformat(),
                "source": source,
            }
    return {
        "ltp": "0",
        "bid": "0",
        "ask": "0",
        "as_of_utc": capture.received_at.isoformat(),
        "source": source,
    }


def _load_previous_bundle(repo_root: Path, session_date: str, before_hhmm: str) -> MinuteBundle | None:
    base = repo_root / "data" / "platter" / "minute" / session_date
    if not base.is_dir():
        return None
    candidates = sorted(
        path
        for path in base.glob("minute_*.json")
        if path.name.replace("minute_", "").replace(".json", "") < before_hhmm
    )
    if not candidates:
        return None
    try:
        return MinuteBundle.model_validate(
            json.loads(candidates[-1].read_text(encoding="utf-8"))
        )
    except (OSError, ValueError):
        return None


def _chain_from_bundle(bundle: MinuteBundle) -> ChainSnapshot:
    return ChainSnapshot.model_validate(bundle.chain)


def _depth_from_bundle(bundle: MinuteBundle) -> DepthSnapshot:
    return DepthSnapshot.model_validate(bundle.depth)


def run_minute_backbone_once(
    repo_root: Path,
    *,
    clock: Clock,
    platter_config: PlatterPipelineConfig | None = None,
) -> Path | None:
    """Fetch chain and depth once, merge, persist minute JSON."""
    pipeline = load_data_pipeline_config(repo_root / "config" / "data_pipeline.yaml")
    platter = platter_config or load_platter_pipeline_config(
        repo_root / "config" / "platter_pipeline.yaml"
    )
    settings = FyersSettings.from_repo_root_with_cache(repo_root)
    feed = FyersMarketFeed(
        settings,
        clock,
        strike_count=platter.symbols.chain_strike_count,
        chain_greeks=pipeline.fyers.chain_greeks,
        history_oi_flag=pipeline.fyers.history_oi_flag,
    )
    now = clock.now_utc()
    minute_start = now.astimezone(_IST).replace(second=0, microsecond=0)
    session_date = minute_start.date().isoformat()
    hhmm = minute_start.strftime("%H%M")
    status = MinuteStatus.OK
    chain: ChainSnapshot | None = None
    depth: DepthSnapshot | None = None
    previous = _load_previous_bundle(repo_root, session_date, hhmm)
    try:
        chain_cap = feed.fetch_option_chain(platter.symbols.nifty_index)
        chain = build_chain_snapshot(chain_cap, symbol=platter.symbols.nifty_index)
    except FyersApiError as exc:
        if fyers_http_status(exc) == 429:
            status = MinuteStatus.DEGRADED
        if previous is not None:
            chain = _chain_from_bundle(previous)
        else:
            chain = None
    try:
        depth_cap = feed.fetch_depth(platter.symbols.nifty_index)
        depth = build_depth_snapshot(depth_cap, symbol=platter.symbols.nifty_index)
    except FyersApiError as exc:
        if fyers_http_status(exc) == 429:
            status = MinuteStatus.DEGRADED
        if previous is not None:
            depth = _depth_from_bundle(previous)
        else:
            depth = None
    if chain is None or depth is None:
        return None
    quotes = feed.fetch_quotes(
        [platter.symbols.nifty_index, platter.symbols.vix_index]
    )
    nifty = _quote_dict(quotes, platter.symbols.nifty_index, source="rest")
    vix = _quote_dict(quotes, platter.symbols.vix_index, source="rest")
    bundle = merge_minute_bundle(
        minute_start_ist=minute_start,
        chain=chain,
        depth=depth,
        nifty_quote=nifty,
        vix_quote=vix,
        status=status,
    )
    path = persist_minute_bundle(repo_root, bundle)
    publish_depth_hint(
        repo_root,
        bid=str(nifty.get("bid") or "0"),
        ask=str(nifty.get("ask") or "0"),
        as_of_utc=datetime.fromisoformat(str(nifty["as_of_utc"])),
    )
    return path
