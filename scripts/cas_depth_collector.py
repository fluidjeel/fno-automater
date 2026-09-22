#!/usr/bin/env python3
"""Paper-only CAS depth collector. Run as a separate process from execution."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from trading.data.cas_depth import CasDepthCollector, load_cas_data_config
from trading.data.fyers.client import FyersMarketFeed
from trading.data.settings import FyersSettings
from trading.domain.clock import WallClock


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _load_settings(root: Path) -> FyersSettings:
    return FyersSettings.from_repo_root_with_cache(root)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to cas_data.yaml",
    )
    parser.add_argument(
        "--duration-seconds",
        type=float,
        default=None,
        help="Override benchmark duration",
    )
    args = parser.parse_args(argv)
    root = _repo_root()
    config_path = args.config or root / "config" / "cas_data.yaml"
    config = load_cas_data_config(config_path)
    if config.cas_live_orders:
        print("cas_live_orders must remain false", file=sys.stderr)
        return 1
    settings = _load_settings(root)
    feed = FyersMarketFeed(settings, WallClock())
    collector = CasDepthCollector(config, settings, repo_root=root, feed=feed)
    duration = args.duration_seconds
    if duration is None:
        duration = float(config.benchmark.duration_minutes * 60)
    result = collector.run(duration_seconds=duration)
    metrics = result.metrics
    print(f"subscribed: {', '.join(result.subscribed_symbols)}")
    print(f"tbt_symbols: {', '.join(result.tbt_symbols)}")
    print(f"mcx_symbols: {', '.join(result.mcx_symbols)}")
    print(f"stopped: {result.stopped_reason}")
    print(f"updates/s: {metrics.updates_per_second():.2f}")
    print(
        f"processed: {metrics.messages_processed} dropped: {metrics.messages_dropped}"
    )
    print(f"max_queue_depth: {metrics.max_queue_depth}")
    latency = metrics.latency_summary()
    print(f"latency avg/p99 ms: {latency['avg_ms']} / {latency['p99_ms']}")
    memory = metrics.memory_summary()
    print(f"memory avg/max mb: {memory['avg_mb']} / {memory['max_mb']}")
    print(f"quality: {result.quality}")
    print(f"mcx_supported: {result.mcx_supported}")
    if result.report_path:
        print(f"report: {result.report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
