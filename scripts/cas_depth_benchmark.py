#!/usr/bin/env python3
"""Run a bounded CAS depth benchmark and emit a markdown report."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from trading.data.cas_depth import CasDepthCollector, load_cas_data_config
from trading.data.cas_depth.collector import CollectorRunResult
from trading.data.fyers.client import FyersMarketFeed
from trading.data.settings import FyersSettings
from trading.domain.clock import WallClock


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _load_settings(root: Path) -> FyersSettings:
    settings = FyersSettings.from_repo_root(root)
    cached = settings.load_cached_token(root)
    if cached and not settings.fyers_access_token:
        settings = settings.model_copy(update={"fyers_access_token": cached})
    return settings


def _markdown_report(result: CollectorRunResult, config_path: Path) -> str:
    metrics = result.metrics
    latency = metrics.latency_summary()
    memory = metrics.memory_summary()
    lines = [
        "# CAS Depth Collector Benchmark",
        "",
        f"Generated: {datetime.now(tz=UTC).isoformat()}",
        f"Config: `{config_path}`",
        "",
        "## Configuration",
        "",
        "- cas_data_mode: DEPTH_ONLY",
        "- cas_live_orders: false",
        f"- stopped_reason: {result.stopped_reason}",
        "",
        "## Symbols",
        "",
        f"- Subscribed: {', '.join(result.subscribed_symbols)}",
        f"- TBT (NSE/NFO): {', '.join(result.tbt_symbols) or 'none'}",
        f"- MCX (data WS): {', '.join(result.mcx_symbols) or 'none'}",
        f"- MCX supported this run: {result.mcx_supported}",
        "",
        "## Throughput",
        "",
        f"- Updates/sec: {metrics.updates_per_second():.3f}",
        f"- Messages received: {metrics.messages_received}",
        f"- Messages processed: {metrics.messages_processed}",
        f"- Messages dropped: {metrics.messages_dropped}",
        f"- Max queue depth: {metrics.max_queue_depth}",
        "",
        "## Latency",
        "",
        f"- Avg processing latency (ms): {latency['avg_ms']}",
        f"- P99 processing latency (ms): {latency['p99_ms']}",
        "",
        "## Resources",
        "",
        f"- Avg memory (MB): {memory['avg_mb']}",
        f"- Max memory (MB): {memory['max_mb']}",
        "",
        "## Quality",
        "",
        f"- Reconnects: {result.quality.get('reconnects', 0)}",
        f"- Sequence gaps: {result.quality.get('sequence_gaps', 0)}",
        f"- Duplicates: {result.quality.get('duplicates', 0)}",
        f"- Stale: {result.quality.get('stale', 0)}",
        f"- Queue overflows: {result.quality.get('queue_overflows', 0)}",
        "",
        "## Fields observed",
        "",
        ", ".join(sorted(metrics.fields_observed)) or "none",
        "",
        "## Execution isolation",
        "",
        "This collector writes only under `data/cas_depth/` and does not open "
        "`data/paper/trading.sqlite` or `data/catalog.duckdb`. It is safe to run "
        "alongside the paper execution session on this VM when memory limits are "
        "respected.",
        "",
        "**Note:** Depth updates are book state, not full order flow. "
        "`trade_aggressor` remains UNKNOWN.",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument(
        "--minutes",
        type=int,
        default=None,
        help="Benchmark duration in minutes (default from config, max 60)",
    )
    args = parser.parse_args(argv)
    root = _repo_root()
    config_path = args.config or root / "config" / "cas_data.yaml"
    config = load_cas_data_config(config_path)
    minutes = args.minutes or config.benchmark.duration_minutes
    minutes = min(max(minutes, 1), 60)
    settings = _load_settings(root)
    feed = FyersMarketFeed(settings, WallClock())
    collector = CasDepthCollector(config, settings, repo_root=root, feed=feed)
    result = collector.run(duration_seconds=float(minutes * 60))
    report_dir = root / config.storage.root / config.benchmark.report_subdir
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    md_path = report_dir / f"{stamp}_BENCHMARK.md"
    md_path.write_text(_markdown_report(result, config_path), encoding="utf-8")
    if result.report_path:
        json_path = report_dir / f"{stamp}_BENCHMARK.json"
        json_path.write_text(
            json.dumps(result.metrics.to_dict(quality=result.quality), indent=2),
            encoding="utf-8",
        )
    print(md_path.read_text(encoding="utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
