"""Append-only InstrumentSpec catalog keyed by trading symbol."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from trading.domain.contracts import InstrumentSpec

__all__ = ["InstrumentSpecStore"]

_LOG = logging.getLogger(__name__)


class InstrumentSpecStore:
    """Persist and look up instrument specifications per segment."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._root.mkdir(parents=True, exist_ok=True)
        self._index: dict[str, InstrumentSpec] | None = None

    def path_for(self, segment: str) -> Path:
        return self._root / f"{segment}.jsonl"

    def write(self, segment: str, specs: Sequence[InstrumentSpec]) -> Path:
        """Replace the segment catalog. A partial master must not shadow a full one."""
        path = self.path_for(segment)
        lines = [spec.model_dump_json() for spec in specs]
        payload = "".join(f"{line}\n" for line in lines)
        temporary = path.with_suffix(".jsonl.tmp")
        temporary.write_text(payload, encoding="utf-8")
        temporary.replace(path)
        self._index = None
        return path

    def load(self, segment: str) -> tuple[InstrumentSpec, ...]:
        path = self.path_for(segment)
        if not path.is_file():
            return ()
        return self._parse_lines(path.read_text(encoding="utf-8").splitlines())

    def sanitize_segment(self, segment: str) -> tuple[int, int]:
        """Drop corrupt JSONL rows and atomically rewrite the segment catalog."""
        path = self.path_for(segment)
        if not path.is_file():
            return 0, 0
        lines = path.read_text(encoding="utf-8").splitlines()
        kept: list[str] = []
        corrupt: list[str] = []
        for raw in lines:
            text = raw.strip()
            if not text:
                continue
            try:
                InstrumentSpec.model_validate_json(text)
            except ValidationError:
                corrupt.append(text)
                continue
            kept.append(text)
        if not corrupt:
            return len(kept), 0
        stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
        quarantine = path.with_name(f"{path.name}.corrupt-{stamp}")
        quarantine.write_text("\n".join(corrupt) + "\n", encoding="utf-8")
        payload = "".join(f"{line}\n" for line in kept)
        temporary = path.with_suffix(".jsonl.tmp")
        temporary.write_text(payload, encoding="utf-8")
        temporary.replace(path)
        self._index = None
        _LOG.warning(
            "sanitized %s: kept %s lines, quarantined %s corrupt rows to %s",
            path.name,
            len(kept),
            len(corrupt),
            quarantine.name,
        )
        return len(kept), len(corrupt)

    def sanitize_all(self) -> dict[str, tuple[int, int]]:
        """Sanitize every ``*.jsonl`` catalog under this store root."""
        results: dict[str, tuple[int, int]] = {}
        for path in sorted(self._root.glob("*.jsonl")):
            segment = path.stem
            results[segment] = self.sanitize_segment(segment)
        stale_tmp = [
            item
            for item in self._root.glob("*.jsonl.tmp")
            if item.is_file()
        ]
        for item in stale_tmp:
            _LOG.warning("removing stale instrument catalog temp file %s", item.name)
            item.unlink(missing_ok=True)
        return results

    @staticmethod
    def _parse_lines(lines: Sequence[str]) -> tuple[InstrumentSpec, ...]:
        specs: list[InstrumentSpec] = []
        for line_no, raw in enumerate(lines, start=1):
            text = raw.strip()
            if not text:
                continue
            try:
                specs.append(InstrumentSpec.model_validate_json(text))
            except ValidationError as exc:
                _LOG.warning(
                    "skipping corrupt instrument catalog line %s: %s",
                    line_no,
                    exc,
                )
        return tuple(specs)

    def invalidate_cache(self) -> None:
        """Drop the in-memory index so the next lookup reloads from disk."""
        self._index = None

    def merge(self, segment: str, specs: Sequence[InstrumentSpec]) -> int:
        """Upsert specs into one segment catalog without dropping existing rows."""
        if not specs:
            return 0
        existing = {spec.trading_symbol: spec for spec in self.load(segment)}
        added = 0
        for spec in specs:
            if spec.trading_symbol not in existing:
                added += 1
            existing[spec.trading_symbol] = spec
        self.write(segment, tuple(existing.values()))
        return added

    def find(self, trading_symbol: str) -> InstrumentSpec | None:
        """Return the spec for one trading symbol across every stored segment.

        Returns None when no catalog has been downloaded, so callers fail closed
        rather than assuming a tick or lot size.
        """
        if self._index is None:
            index: dict[str, InstrumentSpec] = {}
            for spec in self._all():
                index[spec.trading_symbol] = spec
            self._index = index
        return self._index.get(trading_symbol)

    def list_all(self) -> tuple[InstrumentSpec, ...]:
        """Return every stored spec. Empty when no catalog has been downloaded."""
        return self._all()

    def _all(self) -> tuple[InstrumentSpec, ...]:
        specs: list[InstrumentSpec] = []
        for path in sorted(self._root.glob("*.jsonl")):
            specs.extend(self._parse_lines(path.read_text(encoding="utf-8").splitlines()))
        return tuple(specs)
