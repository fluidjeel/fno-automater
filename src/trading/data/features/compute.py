"""Build one feature row JSON from a minute bundle."""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import polars as pl

from trading.data.features.delta_state import load_delta_state, save_delta_state
from trading.data.features.quality import QualityFlag
from trading.data.minute_backbone.models import MinuteBundle, MinuteStatus
from trading.data.m2_agentic_pipeline.config import M2AgenticPipelineConfig
from trading.data.m2_agentic_pipeline.io import atomic_json_write
from trading.data.tick_consumer.models import DeltaState
from trading.news.contracts import EventRiskState, EventRiskStatus

_IST = ZoneInfo("Asia/Kolkata")

__all__ = ["compute_feature_row", "feature_row_path", "persist_feature_row"]


def feature_row_path(repo_root: Path, session_date: str, hhmm: str) -> Path:
    return (
        repo_root
        / "data"
        / "m2_agentic"
        / "features"
        / session_date
        / f"features_{hhmm}.json"
    )


def _read_event_risk(repo_root: Path) -> EventRiskState | None:
    path = repo_root / "data" / "paper" / "event_risk_state.json"
    if not path.is_file():
        return None
    try:
        return EventRiskState.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None


def _bars_frame(repo_root: Path, session_date: str) -> pl.DataFrame | None:
    path = (
        repo_root
        / "data"
        / "m2_agentic"
        / "bars_1m"
        / session_date
        / "bars.parquet"
    )
    if not path.is_file():
        return None
    return pl.read_parquet(path)


def compute_feature_row(
    repo_root: Path,
    bundle: MinuteBundle,
    *,
    config: M2AgenticPipelineConfig,
    delta: DeltaState | None = None,
    mechanical: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compute all v1 feature blocks for one minute."""
    local = bundle.minute_start_ist.astimezone(_IST)
    session_date = local.date().isoformat()
    quality = (
        QualityFlag.OK
        if bundle.status is MinuteStatus.OK
        else QualityFlag.DEGRADED
    )
    spot_ltp = Decimal(str(bundle.nifty_quote.get("ltp") or "0"))
    depth = bundle.depth
    bid_sum = sum(int(depth.get(f"bid_size_{index}") or 0) for index in range(1, 6))
    ask_sum = sum(int(depth.get(f"ask_size_{index}") or 0) for index in range(1, 6))
    denom = bid_sum + ask_sum
    imbalance_l5 = "0"
    if denom > 0:
        imbalance_l5 = str(Decimal(bid_sum - ask_sum) / Decimal(denom))
    bars = _bars_frame(repo_root, session_date)
    poc = vah = val = "0"
    session_vwap = "0"
    if bars is not None and bars.height > 0:
        typical = (
            (pl.col("high").cast(pl.Float64) + pl.col("low").cast(pl.Float64) + pl.col("close").cast(pl.Float64))
            / 3.0
        )
        vol = pl.col("volume").cast(pl.Float64)
        num = (typical * vol).sum()
        den = vol.sum()
        if den and den > 0:
            session_vwap = str(Decimal(str(num / den)))
        top = bars.sort("volume", descending=True).head(1)
        if top.height:
            poc = str(top[0, "close"])
    event = _read_event_risk(repo_root)
    blackout = False
    labels: list[str] = []
    if event is not None:
        labels = [event.state.value]
        blackout = event.state in {
            EventRiskStatus.BLOCK_NEW_ENTRIES,
            EventRiskStatus.MARKET_EMERGENCY,
        }
    lot = config.options.lot_size
    gex_net = Decimal("0")
    chain_rows = bundle.chain.get("strikes") or []
    if isinstance(chain_rows, list):
        for row in chain_rows:
            if not isinstance(row, dict):
                continue
            gamma = Decimal(str(row.get("gamma") or "0"))
            oi = int(row.get("oi") or 0)
            sign = Decimal("1") if row.get("option_type") == "CALL" else Decimal("-1")
            gex_net += sign * gamma * Decimal(oi) * spot_ltp * Decimal(lot)
    mech = mechanical or {
        "trend": "RANGE",
        "trend_score": "0",
        "setup_would_fire": False,
        "setup_direction": "NONE",
        "mode_id": "M2",
        "quality": quality.value,
    }
    if delta is None:
        loaded = load_delta_state(repo_root, session_date)
        if loaded is not None:
            delta = loaded
    delta = delta or DeltaState(session_date=local.date())
    buy = delta.session_buy_volume
    sell = delta.session_sell_volume
    aggression = "0"
    if buy + sell > 0:
        aggression = str(Decimal(buy) / Decimal(buy + sell))
    return {
        "schema_version": "1",
        "minute_start_ist": bundle.minute_start_ist.isoformat(),
        "flow": {
            "session_delta": delta.session_delta,
            "delta_5m": delta.delta_5m,
            "buy_volume": buy,
            "sell_volume": sell,
            "aggression_buy_pct": aggression,
            "large_print_count": 0,
            "quality": quality.value,
        },
        "depth": {
            "imbalance_l1": imbalance_l5,
            "imbalance_l5": imbalance_l5,
            "spread_bps": str(depth.get("spread_bps") or "0"),
            "as_of_utc": depth.get("as_of_utc"),
            "quality": quality.value,
        },
        "structure": {
            "opening_range_15m_high": str(spot_ltp),
            "opening_range_15m_low": str(spot_ltp),
            "opening_range_30m_high": str(spot_ltp),
            "opening_range_30m_low": str(spot_ltp),
            "prior_day_high": "0",
            "prior_day_low": "0",
            "prior_day_close": "0",
            "last_completed_5m": {},
            "move_vs_last_5m": "flat",
            "quality": quality.value,
        },
        "volume_profile": {
            "poc": poc,
            "vah": vah,
            "val": val,
            "quality": quality.value if bars is not None else QualityFlag.MISSING.value,
        },
        "vwap": {
            "session": session_vwap,
            "week": session_vwap,
            "month": session_vwap,
            "dist_session_pct": "0",
            "dist_week_pct": "0",
            "dist_month_pct": "0",
            "quality": quality.value,
        },
        "vol": {
            "rv_5m": "0",
            "rv_15m": "0",
            "rv_60m": "0",
            "iv_atm": "0",
            "rv_iv_ratio": "0",
            "vix_level": str(bundle.vix_quote.get("ltp") or "0"),
            "vix_change_pct": "0",
            "quality": quality.value,
        },
        "options": {
            "max_oi_call_strike": "0",
            "max_oi_put_strike": "0",
            "top_delta_oi_strikes": [],
            "gex_net": str(gex_net),
            "delta_notional_net": "0",
            "atm_iv": "0",
            "skew_25d": "0",
            "oi_change_quality": quality.value,
        },
        "absorption": {
            "level_price": str(spot_ltp),
            "aggressive_volume": 0,
            "price_progress_ticks": 0,
            "flag": False,
            "quality": quality.value,
        },
        "events": {
            "event_state": labels[0] if labels else "UNKNOWN",
            "blackout_active": blackout,
            "calendar_labels": labels,
            "quality": QualityFlag.OK.value if event else QualityFlag.MISSING.value,
        },
        "mechanical": mech,
    }


def persist_feature_row(repo_root: Path, row: dict[str, Any]) -> Path:
    """Write feature row JSON atomically."""
    start = datetime.fromisoformat(str(row["minute_start_ist"]))
    local = start.astimezone(_IST)
    path = feature_row_path(repo_root, local.date().isoformat(), local.strftime("%H%M"))
    atomic_json_write(path, row)
    return path
