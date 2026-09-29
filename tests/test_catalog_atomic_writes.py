"""Catalog Parquet writes are atomic and a torn day file never crashes a cycle.

Incident 29 Sep 2026: ``snapshots-2026-09-29.parquet`` was torn by an in-place
write (footer length 3232321 > file size 5872); every PAPER cycle then crashed
in ``append_snapshots`` -> ``pl.read_parquet`` until the session gave up.
"""

from __future__ import annotations

import logging
import struct
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from tests.test_data_pipeline import _quotes_capture
from trading.data.normalize import normalize_fyers_quotes
from trading.data.storage.catalog import CatalogWriter

DAY = "2026-09-29"


def _catalog(tmp_path: Path) -> CatalogWriter:
    return CatalogWriter(
        tmp_path / "data",
        duckdb_path=tmp_path / "data" / "catalog.duckdb",
    )


def _row(minute: int, snapshot_id: str) -> dict[str, Any]:
    return {
        "snapshot_id": snapshot_id,
        "symbol": "NSE:NIFTY50-INDEX",
        "as_of": f"{DAY}T06:{minute:02d}:00+00:00",
        "decision": "PASS",
        "quality_state": "VALID",
        "permits_new_exposure": True,
        "reason_codes": "[]",
    }


def _incident_footer_bytes(_valid: bytes) -> bytes:
    """Same shape as the Oracle file: 5872 bytes claiming a 3232321-byte footer."""
    body = b"PAR1" + b"\x00" * (5872 - 12)
    return body + struct.pack("<I", 3232321) + b"PAR1"


def _half_written_bytes(valid: bytes) -> bytes:
    return valid[: len(valid) // 2]


@pytest.mark.parametrize(
    "tear", [_incident_footer_bytes, _half_written_bytes], ids=["footer", "half"]
)
def test_torn_snapshot_parquet_is_quarantined_not_fatal(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    tear: Callable[[bytes], bytes],
) -> None:
    catalog = _catalog(tmp_path)
    path = catalog.append_snapshots([_row(0, "SNAP-OLD")])
    assert path is not None
    torn = tear(path.read_bytes())
    path.write_bytes(torn)
    with pytest.raises(Exception):  # noqa: B017 - precondition: file is unreadable
        pl.read_parquet(path)

    with caplog.at_level(logging.WARNING, logger="trading.data.storage.catalog"):
        result = catalog.append_snapshots([_row(1, "SNAP-NEW")])

    assert result == path
    frame = pl.read_parquet(path)
    assert frame["snapshot_id"].to_list() == ["SNAP-NEW"]
    quarantined = sorted(path.parent.glob(f"{path.name}.corrupt-*"))
    assert len(quarantined) == 1
    assert quarantined[0].read_bytes() == torn
    assert "moved aside" in caplog.text

    # Later cycles merge normally on top of the repaired file.
    catalog.append_snapshots([_row(2, "SNAP-NEXT")])
    merged = pl.read_parquet(path).sort("as_of")
    assert merged["snapshot_id"].to_list() == ["SNAP-NEW", "SNAP-NEXT"]


def test_torn_event_parquet_is_quarantined_not_fatal(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    parquet_dir = tmp_path / "data" / "parquet"
    torn_path = parquet_dir / "2026-09-21.parquet"
    torn_path.write_bytes(_incident_footer_bytes(b""))
    event = normalize_fyers_quotes(
        _quotes_capture(datetime(2026, 9, 21, 4, 21, tzinfo=UTC)),
        symbol="NSE:NIFTY50-INDEX",
        normalization_version="1",
        raw_ref="new.json",
    )
    path = catalog.append([event])
    assert path == torn_path
    assert pl.read_parquet(path)["event_id"].to_list() == [event.event_id]
    assert len(list(parquet_dir.glob("2026-09-21.parquet.corrupt-*"))) == 1


def test_failed_write_leaves_previous_file_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = _catalog(tmp_path)
    path = catalog.append_snapshots([_row(0, "SNAP-OLD")])
    assert path is not None
    before = path.read_bytes()

    real_write = pl.DataFrame.write_parquet

    def crash_mid_write(self: pl.DataFrame, file: Any, *args: Any, **kw: Any) -> None:
        real_write(self, file, *args, **kw)
        target = Path(file)
        data = target.read_bytes()
        target.write_bytes(data[: len(data) // 3])  # simulate a torn write
        raise OSError("disk went away mid-write")

    monkeypatch.setattr(pl.DataFrame, "write_parquet", crash_mid_write)
    with pytest.raises(OSError, match="mid-write"):
        catalog.append_snapshots([_row(1, "SNAP-NEW")])
    monkeypatch.undo()

    assert path.read_bytes() == before
    assert pl.read_parquet(path)["snapshot_id"].to_list() == ["SNAP-OLD"]
    leftovers = [p.name for p in path.parent.iterdir() if p.name != path.name]
    assert leftovers == []


def test_failed_first_write_leaves_no_partial_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = _catalog(tmp_path)

    def torn_write(self: pl.DataFrame, file: Any, *args: Any, **kw: Any) -> None:
        Path(file).write_bytes(b"PAR1\x00\x00")
        raise OSError("killed mid-write")

    monkeypatch.setattr(pl.DataFrame, "write_parquet", torn_write)
    with pytest.raises(OSError, match="mid-write"):
        catalog.append_snapshots([_row(0, "SNAP-OLD")])

    parquet_dir = tmp_path / "data" / "parquet"
    assert list(parquet_dir.iterdir()) == []
