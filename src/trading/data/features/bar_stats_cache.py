"""Incremental session bar stats for feature rows (no full bar file scans)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from trading.data.m2_agentic_pipeline.io import atomic_json_write
from trading.data.tick_consumer.models import Bar1m

__all__ = [
    "BarStatsSnapshot",
    "apply_bars",
    "bar_stats_path",
    "load_bar_stats",
    "volume_profile_from_stats",
    "vwap_from_stats",
]


class BarStatsSnapshot(BaseModel):
    """Running session aggregates updated when one-minute bars flush."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"] = "1"
    session_date: str
    vwap_numerator: str = "0"
    vwap_denominator: str = "0"
    poc_price: str = "0"
    poc_volume: int = 0
    bar_count: int = 0


def bar_stats_path(repo_root: Path, session_date: str) -> Path:
    return repo_root / "data" / "m2_agentic" / "state" / f"bar_stats_{session_date}.json"


def load_bar_stats(repo_root: Path, session_date: str) -> BarStatsSnapshot | None:
    path = bar_stats_path(repo_root, session_date)
    if not path.is_file():
        return None
    try:
        import json

        return BarStatsSnapshot.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None


def _typical_price(bar: Bar1m) -> Decimal:
    return (Decimal(bar.high) + Decimal(bar.low) + Decimal(bar.close)) / Decimal("3")


def apply_bars(repo_root: Path, bars: list[Bar1m], *, session_date: str) -> BarStatsSnapshot:
    """Merge completed bars into the session stats file."""
    state = load_bar_stats(repo_root, session_date) or BarStatsSnapshot(session_date=session_date)
    num = Decimal(state.vwap_numerator)
    den = Decimal(state.vwap_denominator)
    poc_price = state.poc_price
    poc_volume = state.poc_volume
    count = state.bar_count
    for bar in bars:
        typical = _typical_price(bar)
        vol = Decimal(bar.volume)
        num += typical * vol
        den += vol
        if bar.volume >= poc_volume:
            poc_volume = bar.volume
            poc_price = bar.close
        count += 1
    updated = BarStatsSnapshot(
        session_date=session_date,
        vwap_numerator=str(num),
        vwap_denominator=str(den),
        poc_price=poc_price,
        poc_volume=poc_volume,
        bar_count=count,
    )
    atomic_json_write(bar_stats_path(repo_root, session_date), updated.model_dump(mode="json"))
    return updated


def vwap_from_stats(stats: BarStatsSnapshot) -> str:
    den = Decimal(stats.vwap_denominator)
    if den <= 0:
        return "0"
    return str(Decimal(stats.vwap_numerator) / den)


def volume_profile_from_stats(stats: BarStatsSnapshot) -> tuple[str, str, str]:
    if stats.bar_count <= 0:
        return "0", "0", "0"
    poc = stats.poc_price
    return poc, poc, poc
