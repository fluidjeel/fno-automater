"""Assemble and store five-minute decision packets."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from trading.data.config import load_data_pipeline_config
from trading.data.features.compute import feature_row_path
from trading.data.minute_backbone.models import MinuteBundle, minute_bundle_path
from trading.data.m2_agentic_pipeline.config import M2AgenticPipelineConfig
from trading.data.m2_agentic_pipeline.io import atomic_json_write
from trading.data.tick_consumer.store import bars_parquet_path, tick_parquet_path
from trading.trade.m2_veto import load_m2_veto_config

_IST = ZoneInfo("Asia/Kolkata")

__all__ = [
    "assemble_m2_agentic_decision",
    "load_minute_bundle_for_mark",
    "persist_m2_agentic_decision",
    "m2_agentic_decision_path",
    "latest_feature_row",
]


def m2_agentic_decision_path(repo_root: Path, session_date: str, mark: datetime) -> Path:
    local = mark.astimezone(_IST)
    stamp = local.strftime("%H%M%S")
    return (
        repo_root
        / "data"
        / "m2_agentic"
        / "decision_packets"
        / session_date
        / f"m2_agentic_{stamp}.json"
    )


def _quote_age_ms(bundle_quote: dict[str, Any], *, now: datetime) -> int:
    raw = bundle_quote.get("as_of_utc")
    if raw is None:
        return 999_999
    try:
        as_of = datetime.fromisoformat(str(raw))
    except ValueError:
        return 999_999
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=now.tzinfo)
    return max(0, int((now - as_of).total_seconds() * 1000))


def _file_age_ms(path: Path, *, now: datetime) -> int | None:
    if not path.is_file():
        return None
    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=now.tzinfo)
    return max(0, int((now - mtime).total_seconds() * 1000))


def _freshness_block(
    repo_root: Path,
    *,
    session_date: str,
    nifty_quote: dict[str, Any],
    bundle: MinuteBundle | None,
    now: datetime,
    veto_seconds: int,
) -> dict[str, Any]:
    quote_age = _quote_age_ms(nifty_quote, now=now)
    quote_ok = quote_age <= veto_seconds * 1000
    chain_age = depth_age = 0
    chain_ok = depth_ok = bundle is not None
    if bundle is not None:
        chain_raw = bundle.chain.get("as_of_utc")
        depth_raw = bundle.depth.get("as_of_utc")
        if chain_raw:
            chain_age = _quote_age_ms({"as_of_utc": chain_raw}, now=now)
            chain_ok = chain_age <= 120_000
        if depth_raw:
            depth_age = _quote_age_ms({"as_of_utc": depth_raw}, now=now)
            depth_ok = depth_age <= 120_000
    tick_path = tick_parquet_path(repo_root, datetime.fromisoformat(session_date).date())
    bar_path = bars_parquet_path(repo_root, datetime.fromisoformat(session_date).date())
    tick_age = _file_age_ms(tick_path, now=now)
    bar_age = _file_age_ms(bar_path, now=now)
    tick_ok = tick_age is not None and tick_age <= 120_000
    bar_1m_ok = bar_age is not None and bar_age <= 120_000
    return {
        "quote_ok": quote_ok,
        "chain_ok": chain_ok,
        "depth_ok": depth_ok,
        "tick_ok": tick_ok,
        "bar_1m_ok": bar_1m_ok,
        "quote_age_ms": quote_age,
        "chain_age_ms": chain_age,
        "depth_age_ms": depth_age,
        "tick_age_ms": tick_age or 999_999,
    }


def assemble_m2_agentic_decision(
    repo_root: Path,
    *,
    m2_agentic_mark_ist: datetime,
    feature_row: dict[str, Any],
    minute_bundle_path: str,
    feature_row_path_value: str,
    chain_snapshot_id: str,
    depth_snapshot_id: str,
    nifty_quote: dict[str, Any],
    config: M2AgenticPipelineConfig,
    now: datetime,
    minute_bundle: MinuteBundle | None = None,
) -> dict[str, Any]:
    """Merge feature blocks into one DecisionPlatter dict."""
    load_data_pipeline_config(repo_root / "config" / "data_pipeline.yaml")
    veto = load_m2_veto_config()
    session_date = m2_agentic_mark_ist.astimezone(_IST).date().isoformat()
    freshness = _freshness_block(
        repo_root,
        session_date=session_date,
        nifty_quote=nifty_quote,
        bundle=minute_bundle,
        now=now,
        veto_seconds=veto.quote_max_age_seconds,
    )
    ltp = str(nifty_quote.get("ltp") or "")
    fail_closed = not freshness["quote_ok"] or ltp in {"", "0"}
    return {
        "schema_version": "1",
        "m2_agentic_id": str(uuid.uuid4()),
        "m2_agentic_mark_ist": m2_agentic_mark_ist.isoformat(),
        "session_date": session_date,
        "as_of_utc": now.isoformat(),
        "fail_closed": fail_closed,
        "freshness": freshness,
        "spot": {
            "symbol": config.symbols.nifty_index,
            "ltp": ltp,
            "bid": str(nifty_quote.get("bid") or "0"),
            "ask": str(nifty_quote.get("ask") or "0"),
            "spread_bps": feature_row.get("depth", {}).get("spread_bps", "0"),
            "source": str(nifty_quote.get("source") or "rest"),
        },
        "flow": feature_row.get("flow", {}),
        "depth": feature_row.get("depth", {}),
        "structure": feature_row.get("structure", {}),
        "volume_profile": feature_row.get("volume_profile", {}),
        "vwap": feature_row.get("vwap", {}),
        "vol": feature_row.get("vol", {}),
        "options": feature_row.get("options", {}),
        "absorption": feature_row.get("absorption", {}),
        "events": feature_row.get("events", {}),
        "mechanical": feature_row.get("mechanical", {}),
        "lineage": {
            "minute_bundle_path": minute_bundle_path,
            "feature_row_path": feature_row_path_value,
            "chain_snapshot_id": chain_snapshot_id,
            "depth_snapshot_id": depth_snapshot_id,
            "tick_store_session_date": session_date,
            "feature_calc_version": config.calculation_version,
            "gex_formula_version": config.options.gex_formula_version,
        },
    }


def persist_m2_agentic_decision(repo_root: Path, platter: dict[str, Any]) -> Path:
    """Atomic write for M2 agentic JSON."""
    mark = datetime.fromisoformat(str(platter["m2_agentic_mark_ist"]))
    path = m2_agentic_decision_path(repo_root, str(platter["session_date"]), mark)
    atomic_json_write(path, platter)
    return path


def load_minute_bundle_for_mark(repo_root: Path, mark: datetime) -> tuple[MinuteBundle, Path] | None:
    """Load minute bundle matching the platter mark minute."""
    local = mark.astimezone(_IST)
    path = minute_bundle_path(repo_root, local.date().isoformat(), local.strftime("%H%M"))
    if not path.is_file():
        return None
    bundle = MinuteBundle.model_validate(json.loads(path.read_text(encoding="utf-8")))
    return bundle, path


def latest_feature_row(repo_root: Path, mark: datetime) -> tuple[dict[str, Any], Path] | None:
    """Load feature row at or before the platter mark."""
    local = mark.astimezone(_IST)
    session = local.date().isoformat()
    target = local.strftime("%H%M")
    base = repo_root / "data" / "m2_agentic" / "features" / session
    if not base.is_dir():
        return None
    candidates = sorted(
        path
        for path in base.glob("features_*.json")
        if path.stem.replace("features_", "") <= target
    )
    if not candidates:
        return None
    path = candidates[-1]
    return json.loads(path.read_text(encoding="utf-8")), path
