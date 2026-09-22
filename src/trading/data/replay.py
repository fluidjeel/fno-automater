"""Deterministic replay of stored canonical events."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from trading.config import load_config
from trading.data.config import (
    DataPipelineConfig,
    UnderlyingConfig,
    load_data_pipeline_config,
)
from trading.data.cycle import build_cycle_snapshot
from trading.data.events import CanonicalMarketEvent
from trading.data.quality import assess_combined_snapshot, assess_index_snapshot
from trading.data.snapshot_builder import MarketSnapshotBuilder
from trading.data.storage.parquet_store import JsonlEventStore
from trading.domain.contracts import FeatureSnapshot

__all__ = ["ReplayEngine", "ReplayResult"]


@dataclass(frozen=True, slots=True)
class ReplayResult:
    """Snapshots rebuilt from storage."""

    symbol: str
    snapshots: tuple[FeatureSnapshot, ...]


def _group_events(
    events: list[CanonicalMarketEvent],
) -> list[list[CanonicalMarketEvent]]:
    """Group events from the same fetch cycle by receive_time proximity."""
    if not events:
        return []
    ordered = sorted(events, key=lambda event: event.receive_time)
    groups: list[list[CanonicalMarketEvent]] = []
    current: list[CanonicalMarketEvent] = [ordered[0]]
    for event in ordered[1:]:
        gap = (event.receive_time - current[-1].receive_time).total_seconds()
        if gap > 1.0:
            groups.append(current)
            current = [event]
        else:
            current.append(event)
    groups.append(current)
    return groups


def _latest_in_group(
    group: list[CanonicalMarketEvent],
    event_type: str,
) -> CanonicalMarketEvent | None:
    matches = [event for event in group if event.event_type == event_type]
    return matches[-1] if matches else None


class ReplayEngine:
    """Rebuild FeatureSnapshots from canonical events without network I/O."""

    def __init__(
        self,
        *,
        pipeline_config: DataPipelineConfig,
        store: JsonlEventStore,
        config_version: str,
        config_checksum: str,
        code_version: str = "0.1.0",
    ) -> None:
        self._pipeline_config = pipeline_config
        self._store = store
        self._config_version = config_version
        self._config_checksum = config_checksum
        self._code_version = code_version
        freshness = pipeline_config.freshness
        self._chain_max_age_ms = freshness.get("option_chain_max_age_ms", {}).get(
            "5m", 120_000
        )
        self._quote_max_age_ms = freshness.get("quote_max_age_ms", {}).get("5m", 60_000)
        self._bar_max_age_ms = freshness.get("bar_max_age_ms", {}).get("5m", 300_000)
        self._depth_max_age_ms = freshness.get("depth_max_age_ms", {}).get("5m", 60_000)

    def replay(
        self,
        underlying: UnderlyingConfig,
        *,
        start: datetime,
        end: datetime,
    ) -> ReplayResult:
        events = list(
            self._store.read_canonical(
                symbol=underlying.symbol,
                start=start,
                end=end,
            )
        )
        builder = MarketSnapshotBuilder(
            underlying,
            config_version=self._config_version,
            config_checksum=self._config_checksum,
            code_version=self._code_version,
            greeks_calculation_version=(
                self._pipeline_config.fyers.greeks_calculation_version
            ),
        )
        snapshots: list[FeatureSnapshot] = []
        for group in _group_events(events):
            calculation_time = group[-1].receive_time
            if underlying.fetch_option_chain:
                chain = _latest_in_group(group, "OPTION_CHAIN_SNAPSHOT")
                if chain is None:
                    continue
                quality = assess_combined_snapshot(
                    chain=chain,
                    quote=_latest_in_group(group, "QUOTE_SNAPSHOT"),
                    bar=_latest_in_group(group, "BAR_SNAPSHOT"),
                    now=calculation_time,
                    chain_max_age_ms=self._chain_max_age_ms,
                    quote_max_age_ms=self._quote_max_age_ms,
                    bar_max_age_ms=self._bar_max_age_ms,
                    depth=_latest_in_group(group, "DEPTH_SNAPSHOT"),
                    market_status=_latest_in_group(group, "MARKET_STATUS"),
                    depth_max_age_ms=self._depth_max_age_ms,
                    session=self._pipeline_config.session,
                    quality_config=self._pipeline_config.quality,
                )
                index_only = False
            else:
                quote = _latest_in_group(group, "QUOTE_SNAPSHOT")
                if quote is None:
                    continue
                quality = assess_index_snapshot(
                    quote=quote,
                    bar=_latest_in_group(group, "BAR_SNAPSHOT"),
                    now=calculation_time,
                    quote_max_age_ms=self._quote_max_age_ms,
                    bar_max_age_ms=self._bar_max_age_ms,
                    depth=_latest_in_group(group, "DEPTH_SNAPSHOT"),
                    market_status=_latest_in_group(group, "MARKET_STATUS"),
                    depth_max_age_ms=self._depth_max_age_ms,
                    session=self._pipeline_config.session,
                    quality_config=self._pipeline_config.quality,
                )
                index_only = True
            snapshot = build_cycle_snapshot(
                group,
                quality=quality,
                as_of=calculation_time,
                builder=builder,
                index_only=index_only,
            )
            if snapshot is not None:
                snapshots.append(snapshot)
        return ReplayResult(symbol=underlying.symbol, snapshots=tuple(snapshots))


def build_replay_engine(repo_root: Path) -> ReplayEngine:
    config_path = repo_root / "config" / "data_pipeline.yaml"
    pipeline_config = load_data_pipeline_config(config_path)
    app = load_config(repo_root / "config" / "base.yaml")
    store = JsonlEventStore(repo_root / pipeline_config.storage.root)
    return ReplayEngine(
        pipeline_config=pipeline_config,
        store=store,
        config_version=app.version,
        config_checksum=app.checksum,
    )
