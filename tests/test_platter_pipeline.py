"""End-to-end platter pipeline wiring and stale-quote assembly."""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from trading.data.features.compute import persist_feature_row
from trading.data.minute_backbone.models import MinuteBundle, MinuteStatus
from trading.data.platter.assemble import assemble_platter, latest_feature_row, load_minute_bundle_for_mark
from trading.data.platter_pipeline.config import load_platter_pipeline_config
from trading.data.platter_pipeline.depth_hint import publish_depth_hint, read_depth_hint
from trading.data.platter_pipeline.orchestrate import run_minute_and_features
from trading.data.tick_consumer.ws_multiplex import _apply_depth_hint

_REPO = Path(__file__).resolve().parents[1]
_IST = ZoneInfo("Asia/Kolkata")


@pytest.fixture
def platter_repo(tmp_path: Path) -> Path:
    """Minimal repo tree with configs required by assemble."""
    (tmp_path / "config").mkdir(parents=True)
    for name in ("platter_pipeline.yaml", "data_pipeline.yaml", "m2_veto.yaml"):
        shutil.copy(_REPO / "config" / name, tmp_path / "config" / name)
    return tmp_path


def test_persist_feature_row_uses_atomic_write(platter_repo: Path) -> None:
    mark = datetime(2026, 10, 31, 9, 15, tzinfo=_IST)
    row = {
        "minute_start_ist": mark.isoformat(),
        "flow": {},
        "structure": {},
        "depth": {},
        "volume_profile": {},
        "vwap": {},
        "vol": {},
        "options": {},
        "absorption": {},
        "events": {},
        "mechanical": {},
    }
    path = persist_feature_row(platter_repo, row)
    assert path.is_file()
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded["minute_start_ist"] == mark.isoformat()


def test_latest_feature_row_at_or_before_mark(platter_repo: Path) -> None:
    session = "2026-10-31"
    base = platter_repo / "data" / "platter" / "features" / session
    base.mkdir(parents=True)
    (base / "features_0910.json").write_text('{"minute_start_ist": "x"}', encoding="utf-8")
    (base / "features_0920.json").write_text('{"minute_start_ist": "y"}', encoding="utf-8")
    mark = datetime(2026, 10, 31, 9, 15, tzinfo=_IST)
    loaded = latest_feature_row(platter_repo, mark)
    assert loaded is not None
    _, path = loaded
    assert path.name == "features_0910.json"


def test_stale_minute_quote_sets_fail_closed(platter_repo: Path) -> None:
    config = load_platter_pipeline_config(platter_repo / "config" / "platter_pipeline.yaml")
    now = datetime(2026, 10, 31, 4, 0, tzinfo=UTC)
    mark = now.astimezone(_IST)
    stale_as_of = (now - timedelta(seconds=120)).isoformat()
    nifty = {
        "ltp": "24500.00",
        "bid": "24499",
        "ask": "24501",
        "as_of_utc": stale_as_of,
        "source": "rest",
    }
    bundle = MinuteBundle(
        minute_start_ist=mark.replace(second=0, microsecond=0),
        status=MinuteStatus.OK,
        nifty_quote=nifty,
        vix_quote={"ltp": "12", "as_of_utc": stale_as_of},
        chain_snapshot_id="c1",
        depth_snapshot_id="d1",
        chain={"as_of_utc": stale_as_of},
        depth={"as_of_utc": stale_as_of},
    )
    feature_row = {
        "depth": {"spread_bps": "1"},
        "structure": {"opening_range_15m_high": "99999"},
        "flow": {},
        "volume_profile": {},
        "vwap": {},
        "vol": {},
        "options": {},
        "absorption": {},
        "events": {"blackout_active": False},
        "mechanical": {"setup_direction": "UP"},
    }
    platter = assemble_platter(
        platter_repo,
        platter_mark_ist=mark,
        feature_row=feature_row,
        minute_bundle_path="data/platter/minute/2026-10-31/minute_0915.json",
        feature_row_path_value="data/platter/features/2026-10-31/features_0915.json",
        chain_snapshot_id="c1",
        depth_snapshot_id="d1",
        nifty_quote=nifty,
        config=config,
        now=now,
        minute_bundle=bundle,
    )
    assert platter["fail_closed"] is True
    assert platter["spot"]["ltp"] == "24500.00"


def test_fresh_minute_quote_not_fail_closed_despite_structure_noise(platter_repo: Path) -> None:
    config = load_platter_pipeline_config(platter_repo / "config" / "platter_pipeline.yaml")
    now = datetime(2026, 10, 31, 4, 0, tzinfo=UTC)
    mark = now.astimezone(_IST)
    fresh_as_of = (now - timedelta(seconds=5)).isoformat()
    nifty = {
        "ltp": "24500.00",
        "bid": "24499",
        "ask": "24501",
        "as_of_utc": fresh_as_of,
        "source": "rest",
    }
    bundle = MinuteBundle(
        minute_start_ist=mark.replace(second=0, microsecond=0),
        status=MinuteStatus.OK,
        nifty_quote=nifty,
        vix_quote={"ltp": "12", "as_of_utc": fresh_as_of},
        chain_snapshot_id="c1",
        depth_snapshot_id="d1",
        chain={"as_of_utc": fresh_as_of},
        depth={"as_of_utc": fresh_as_of},
    )
    feature_row = {
        "depth": {"spread_bps": "1"},
        "structure": {"opening_range_15m_high": "99999"},
        "flow": {},
        "volume_profile": {},
        "vwap": {},
        "vol": {},
        "options": {},
        "absorption": {},
        "events": {"blackout_active": False},
        "mechanical": {"setup_direction": "UP"},
    }
    platter = assemble_platter(
        platter_repo,
        platter_mark_ist=mark,
        feature_row=feature_row,
        minute_bundle_path="data/platter/minute/2026-10-31/minute_0915.json",
        feature_row_path_value="data/platter/features/2026-10-31/features_0915.json",
        chain_snapshot_id="c1",
        depth_snapshot_id="d1",
        nifty_quote=nifty,
        config=config,
        now=now,
        minute_bundle=bundle,
    )
    assert platter["fail_closed"] is False


def test_load_minute_bundle_for_mark(platter_repo: Path) -> None:
    mark = datetime(2026, 10, 31, 9, 15, tzinfo=_IST)
    path = (
        platter_repo
        / "data"
        / "platter"
        / "minute"
        / "2026-10-31"
        / "minute_0915.json"
    )
    path.parent.mkdir(parents=True)
    bundle = MinuteBundle(
        minute_start_ist=mark.replace(second=0, microsecond=0),
        status=MinuteStatus.OK,
        nifty_quote={"ltp": "1", "as_of_utc": mark.isoformat()},
        vix_quote={"ltp": "1"},
        chain_snapshot_id="c",
        depth_snapshot_id="d",
        chain={},
        depth={},
    )
    path.write_text(bundle.model_dump_json(indent=2), encoding="utf-8")
    loaded = load_minute_bundle_for_mark(platter_repo, mark)
    assert loaded is not None
    got, got_path = loaded
    assert got.nifty_quote["ltp"] == "1"
    assert got_path == path


def test_depth_hint_round_trip(platter_repo: Path) -> None:
    now = datetime(2026, 10, 31, 4, 0, tzinfo=UTC)
    publish_depth_hint(platter_repo, bid="100", ask="101", as_of_utc=now)
    cache: dict[str, object] = {"bid": None, "ask": None, "as_of": None}
    _apply_depth_hint(platter_repo, cache)
    assert cache["bid"] == "100"
    assert cache["ask"] == "101"
    assert read_depth_hint(platter_repo) is not None


def test_run_minute_and_features_chains_both_steps(platter_repo: Path) -> None:
    minute = platter_repo / "data/platter/minute/2026-10-31/minute_1000.json"
    feature = platter_repo / "data/platter/features/2026-10-31/features_1000.json"
    minute.parent.mkdir(parents=True, exist_ok=True)
    feature.parent.mkdir(parents=True, exist_ok=True)

    minute.write_text(
        MinuteBundle(
            minute_start_ist=datetime(2026, 10, 31, 10, 0, tzinfo=_IST),
            status=MinuteStatus.OK,
            nifty_quote={"ltp": "1"},
            vix_quote={"ltp": "1"},
            chain_snapshot_id="c",
            depth_snapshot_id="d",
            chain={},
            depth={},
        ).model_dump_json(),
        encoding="utf-8",
    )

    class _Clock:
        def now_utc(self) -> datetime:
            return datetime(2026, 10, 31, 4, 30, tzinfo=UTC)

    with (
        patch(
            "trading.data.platter_pipeline.orchestrate.run_minute_backbone_once",
            return_value=minute,
        ),
        patch(
            "trading.data.platter_pipeline.orchestrate.compute_feature_row",
            return_value={"minute_start_ist": "2026-10-31T10:00:00+05:30"},
        ),
        patch(
            "trading.data.platter_pipeline.orchestrate.persist_feature_row",
            return_value=feature,
        ),
        patch(
            "trading.data.platter_pipeline.orchestrate.load_delta_state",
            return_value=None,
        ),
    ):
        result = run_minute_and_features(platter_repo, clock=_Clock())
    assert result == (minute, feature)
