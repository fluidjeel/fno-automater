"""Focused tests for paper-only CAS depth collector."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from trading.data.cas_depth.adapter import CasDepthPaperAdapter
from trading.data.cas_depth.config import (
    CasDataConfig,
    CasSymbolsConfig,
    load_cas_data_config,
)
from trading.data.cas_depth.contracts import (
    DepthLevel,
    DepthQualityIssue,
    NormalizedDepthUpdate,
    TradeAggressor,
)
from trading.data.cas_depth.features import (
    FEATURE_MICROPRICE,
    compute_depth_only_features,
    depth_features_complete,
)
from trading.data.cas_depth.normalize import normalize_data_ws_depth, sort_levels
from trading.data.cas_depth.quality import DepthQualityTracker
from trading.data.cas_depth.storage import CasDepthStorage


def _update(
    *,
    symbol: str = "NSE:NIFTY26SEP24500CE",
    sequence: int | None = 1,
    bid_price: Decimal = Decimal("120.00"),
    ask_price: Decimal = Decimal("120.50"),
) -> NormalizedDepthUpdate:
    now = datetime(2026, 9, 20, 9, 30, tzinfo=UTC)
    bids = (
        DepthLevel(price=bid_price, quantity=1000, orders=5),
        DepthLevel(price=bid_price - Decimal("0.50"), quantity=500, orders=2),
    )
    asks = (
        DepthLevel(price=ask_price, quantity=800, orders=4),
        DepthLevel(price=ask_price + Decimal("0.50"), quantity=400, orders=2),
    )
    return NormalizedDepthUpdate(
        symbol=symbol,
        exchange_timestamp=now,
        receive_timestamp=now,
        sequence=sequence,
        bid_levels=bids,
        ask_levels=asks,
        trade_aggressor=TradeAggressor.UNKNOWN,
        is_snapshot=True,
        feed="tbt_ws",
    )


def test_sort_levels_orders_by_price() -> None:
    """Bid/ask levels must be normalized by price, not provider array order."""
    bids = [
        DepthLevel(price=Decimal("100.00"), quantity=1),
        DepthLevel(price=Decimal("101.00"), quantity=2),
    ]
    asks = [
        DepthLevel(price=Decimal("102.00"), quantity=1),
        DepthLevel(price=Decimal("101.50"), quantity=2),
    ]
    sorted_bids, sorted_asks = sort_levels(bids, asks)
    assert sorted_bids[0].price == Decimal("101.00")
    assert sorted_asks[0].price == Decimal("101.50")


def test_normalize_data_ws_depth_from_unordered_levels() -> None:
    """Data-socket depth maps numbered levels into a normalized book."""
    now = datetime(2026, 9, 20, 9, 30, tzinfo=UTC)
    message = {
        "symbol": "MCX:GOLDM25OCTFUT",
        "bid_price1": 75000.0,
        "bid_size1": 10,
        "ask_price1": 75010.0,
        "ask_size1": 8,
        "type": "dp",
    }
    update = normalize_data_ws_depth(message, receive_time=now)
    assert update is not None
    assert update.bid_levels[0].price == Decimal("75000.00")
    assert update.trade_aggressor is TradeAggressor.UNKNOWN


def test_depth_only_features_computed_without_aggressor() -> None:
    """DEPTH_ONLY features omit trade-flow inference; aggressor stays UNKNOWN."""
    current = _update()
    prior = _update(bid_price=Decimal("119.50"), ask_price=Decimal("120.00"))
    features = compute_depth_only_features(current, prior)
    assert depth_features_complete(features)
    assert FEATURE_MICROPRICE in features


def test_quality_tracker_detects_duplicate_and_gap() -> None:
    """Quality tracker records duplicates and sequence gaps."""
    tracker = DepthQualityTracker(max_stale_seconds=5.0)
    now = datetime(2026, 9, 20, 9, 30, tzinfo=UTC)
    first = _update(sequence=1)
    second = _update(sequence=1)
    third = _update(sequence=4)
    tracker.inspect(first, now=now)
    issues2 = tracker.inspect(second, now=now)
    issues3 = tracker.inspect(third, now=now)
    assert DepthQualityIssue.DUPLICATE in issues2
    assert DepthQualityIssue.SEQUENCE_GAP in issues3
    assert tracker.sequence_gaps >= 2


def test_adapter_abstains_on_stale_depth(tmp_path: Path) -> None:
    """CAS abstains when depth is stale; does not infer aggressor."""
    adapter = CasDepthPaperAdapter(tmp_path / "cas_depth")
    stale_time = datetime(2026, 9, 20, 9, 0, tzinfo=UTC)
    now = stale_time + timedelta(seconds=30)
    update = _update().model_copy(update={"exchange_timestamp": stale_time})
    snapshot = adapter.publish(
        update,
        None,
        quality_issues=(),
        snapshot_id="snap-1",
        now=now,
        max_stale_seconds=5.0,
    )
    assert snapshot.abstain is True
    assert snapshot.trade_aggressor is TradeAggressor.UNKNOWN
    assert adapter.should_abstain() is True


def test_storage_writes_outside_execution_db(tmp_path: Path) -> None:
    """CAS depth storage uses an isolated partition."""
    storage = CasDepthStorage(tmp_path / "cas_depth")
    update = _update()
    storage.append_normalized(update, flush=True)
    files = list((tmp_path / "cas_depth" / "normalized").glob("*.jsonl"))
    assert files
    assert not (tmp_path / "paper" / "trading.sqlite").exists()


def test_config_feature_flags(tmp_path: Path) -> None:
    """Config enforces DEPTH_ONLY mode and cas_live_orders=false."""
    config = CasDataConfig(
        schema_version="1",
        cas_data_mode="DEPTH_ONLY",
        cas_live_orders=False,
        symbols=CasSymbolsConfig(index_or_future="NSE:NIFTY50-INDEX"),
    )
    assert config.cas_data_mode == "DEPTH_ONLY"
    assert config.cas_live_orders is False


def test_load_cas_data_config_from_repo() -> None:
    """Repository config/cas_data.yaml validates."""
    root = Path(__file__).resolve().parents[1]
    config = load_cas_data_config(root / "config" / "cas_data.yaml")
    assert config.cas_data_mode == "DEPTH_ONLY"
    assert config.cas_live_orders is False
