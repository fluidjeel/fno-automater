"""Resource and throughput metrics for the CAS depth collector."""

from __future__ import annotations

import json
import resource
import statistics
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

__all__ = ["CollectorMetrics", "ResourceSnapshot"]


@dataclass
class ResourceSnapshot:
    """Point-in-time process resource reading."""

    timestamp: datetime
    memory_mb: float
    user_cpu_seconds: float


@dataclass
class CollectorMetrics:
    """Aggregated collector metrics for benchmark reporting."""

    started_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
    messages_received: int = 0
    messages_processed: int = 0
    messages_dropped: int = 0
    max_queue_depth: int = 0
    processing_latencies_ms: list[float] = field(default_factory=list)
    resource_samples: list[ResourceSnapshot] = field(default_factory=list)
    subscribed_symbols: tuple[str, ...] = ()
    mcx_supported: bool = False
    fields_observed: set[str] = field(default_factory=set)

    def record_queue_depth(self, depth: int) -> None:
        self.max_queue_depth = max(self.max_queue_depth, depth)

    def record_latency(self, latency_ms: float) -> None:
        self.processing_latencies_ms.append(latency_ms)
        if len(self.processing_latencies_ms) > 10_000:
            self.processing_latencies_ms = self.processing_latencies_ms[-5_000:]

    def sample_resources(self) -> ResourceSnapshot:
        usage = resource.getrusage(resource.RUSAGE_SELF)
        # ru_maxrss is kilobytes on macOS and bytes on Linux.
        if sys.platform == "darwin":
            memory_mb = usage.ru_maxrss / (1024 * 1024)
        else:
            memory_mb = usage.ru_maxrss / 1024
        snapshot = ResourceSnapshot(
            timestamp=datetime.now(tz=UTC),
            memory_mb=memory_mb,
            user_cpu_seconds=usage.ru_utime + usage.ru_stime,
        )
        self.resource_samples.append(snapshot)
        if len(self.resource_samples) > 1000:
            self.resource_samples = self.resource_samples[-500:]
        return snapshot

    def updates_per_second(self) -> float:
        elapsed = (datetime.now(tz=UTC) - self.started_at).total_seconds()
        if elapsed <= 0:
            return 0.0
        return self.messages_processed / elapsed

    def latency_summary(self) -> dict[str, float | None]:
        if not self.processing_latencies_ms:
            return {"avg_ms": None, "p99_ms": None}
        sorted_vals = sorted(self.processing_latencies_ms)
        p99_index = max(int(len(sorted_vals) * 0.99) - 1, 0)
        return {
            "avg_ms": statistics.mean(sorted_vals),
            "p99_ms": sorted_vals[p99_index],
        }

    def memory_summary(self) -> dict[str, float | None]:
        if not self.resource_samples:
            return {"avg_mb": None, "max_mb": None}
        values = [sample.memory_mb for sample in self.resource_samples]
        return {
            "avg_mb": statistics.mean(values),
            "max_mb": max(values),
        }

    def to_dict(self, *, quality: dict[str, Any] | None = None) -> dict[str, Any]:
        latency = self.latency_summary()
        memory = self.memory_summary()
        return {
            "started_at": self.started_at.isoformat(),
            "ended_at": datetime.now(tz=UTC).isoformat(),
            "subscribed_symbols": list(self.subscribed_symbols),
            "updates_per_second": self.updates_per_second(),
            "messages_received": self.messages_received,
            "messages_processed": self.messages_processed,
            "messages_dropped": self.messages_dropped,
            "max_queue_depth": self.max_queue_depth,
            "avg_processing_latency_ms": latency["avg_ms"],
            "p99_processing_latency_ms": latency["p99_ms"],
            "avg_memory_mb": memory["avg_mb"],
            "max_memory_mb": memory["max_mb"],
            "mcx_supported": self.mcx_supported,
            "fields_observed": sorted(self.fields_observed),
            "quality": quality or {},
        }

    def write_report(self, path: Path, *, quality: dict[str, Any] | None = None) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(quality=quality), indent=2), encoding="utf-8")


class LatencyTracker:
    """Rolling window for inter-arrival and processing latency."""

    def __init__(self, window: int = 1000) -> None:
        self._window = window
        self._processing: deque[float] = deque(maxlen=window)
        self._last_receive: datetime | None = None

    def mark_receive(self, now: datetime) -> float | None:
        if self._last_receive is None:
            self._last_receive = now
            return None
        delta = (now - self._last_receive).total_seconds() * 1000.0
        self._last_receive = now
        return delta

    def mark_processed(self, started: float) -> float:
        elapsed = (time.monotonic() - started) * 1000.0
        self._processing.append(elapsed)
        return elapsed
