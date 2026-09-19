"""Append-only InstrumentSpec catalog keyed by trading symbol."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from trading.domain.contracts import InstrumentSpec

__all__ = ["InstrumentSpecStore"]


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
        path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
        self._index = None
        return path

    def load(self, segment: str) -> tuple[InstrumentSpec, ...]:
        path = self.path_for(segment)
        if not path.is_file():
            return ()
        specs: list[InstrumentSpec] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            text = line.strip()
            if text:
                specs.append(InstrumentSpec.model_validate_json(text))
        return tuple(specs)

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
            for line in path.read_text(encoding="utf-8").splitlines():
                text = line.strip()
                if text:
                    specs.append(InstrumentSpec.model_validate_json(text))
        return tuple(specs)
