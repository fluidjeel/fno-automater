"""Self-hosted option chain recorder for Indian derivatives.

Polls option chains periodically and writes partitioned Parquet files locally
under data/recorded_chains/underlying={SYM}/date={YYYY-MM-DD}/.
Eliminates reliance on costly commercial tick-data subscriptions.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Protocol

import polars as pl

from trading.domain.clock import Clock, WallClock

logger = logging.getLogger(__name__)

__all__ = ["ChainFetchable", "OptionChainRecorder"]


class ChainFetchable(Protocol):
    """Protocol for fetching an option chain snapshot."""

    def fetch_option_chain(self, symbol: str) -> Any: ...


class OptionChainRecorder:
    """Records option chains into partitioned Parquet files."""

    def __init__(
        self,
        feed: ChainFetchable,
        output_dir: Path | str = "data/recorded_chains",
        clock: Clock | None = None,
    ) -> None:
        self._feed = feed
        self._output_dir = Path(output_dir)
        self._clock = clock or WallClock()

    @property
    def output_dir(self) -> Path:
        return self._output_dir

    def record_chain(self, symbol: str) -> Path:
        """Fetch and record a single option chain snapshot to Parquet."""
        now = self._clock.now_utc()
        raw_capture = self._feed.fetch_option_chain(symbol)

        # Extract payload dict
        payload = getattr(raw_capture, "payload", raw_capture)
        data = payload.get("data", payload) if isinstance(payload, dict) else {}
        if not isinstance(data, dict):
            data = {}

        rows: list[dict[str, Any]] = []
        raw_rows = data.get("optionsChain") or data.get("options_chain") or []
        if isinstance(raw_rows, list):
            rows = [r for r in raw_rows if isinstance(r, dict)]

        # Clean symbol for filesystem path
        clean_symbol = re.sub(r"[^a-zA-Z0-9_-]", "_", symbol)
        date_str = now.strftime("%Y-%m-%d")
        time_str = now.strftime("%H%M%S")

        target_dir = (
            self._output_dir / f"underlying={clean_symbol}" / f"date={date_str}"
        )
        target_dir.mkdir(parents=True, exist_ok=True)
        file_path = target_dir / f"chain_{time_str}.parquet"

        if rows:
            # Add metadata columns
            for r in rows:
                r["recorded_at"] = now.isoformat()
                r["underlying_symbol"] = symbol

            df = pl.DataFrame(rows)
        else:
            df = pl.DataFrame(
                {
                    "recorded_at": [now.isoformat()],
                    "underlying_symbol": [symbol],
                    "empty": [True],
                }
            )

        df.write_parquet(file_path, compression="snappy")
        logger.info(
            "Recorded option chain for %s (%d rows) -> %s", symbol, len(rows), file_path
        )
        return file_path

    def record_all(self, symbols: Sequence[str]) -> list[Path]:
        """Record all given underlying symbols sequentially."""
        recorded_paths: list[Path] = []
        for symbol in symbols:
            try:
                p = self.record_chain(symbol)
                recorded_paths.append(p)
            except Exception:
                logger.exception("Failed to record option chain for %s", symbol)
        return recorded_paths

    def poll_and_record(
        self,
        symbols: Sequence[str],
        *,
        interval_seconds: int = 60,
        max_iterations: int | None = 1,
        on_record: Callable[[Path], None] | None = None,
    ) -> list[Path]:
        """Poll and record at regular intervals."""
        all_paths: list[Path] = []
        iterations = 0

        while max_iterations is None or iterations < max_iterations:
            batch = self.record_all(symbols)
            all_paths.extend(batch)
            for p in batch:
                if on_record is not None:
                    on_record(p)
            iterations += 1
            if max_iterations is not None and iterations >= max_iterations:
                break
            time.sleep(interval_seconds)

        return all_paths

    @staticmethod
    def load_chain(path: Path | str) -> pl.DataFrame:
        """Read a recorded option chain Parquet file into a Polars DataFrame."""
        return pl.read_parquet(Path(path))
