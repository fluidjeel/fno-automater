"""DISC-A16: CAS depth dedup, DUPLICATE flag, top5 imbalance, multi-symbol health."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from trading.data.cas_depth.adapter import CasDepthPaperAdapter
from trading.data.cas_depth.contracts import (
    DepthLevel,
    DepthQualityIssue,
    NormalizedDepthUpdate,
    TradeAggressor,
)
from trading.data.cas_depth.features import (
    FEATURE_TOP5_IMBALANCE,
    FEATURE_TOP50_IMBALANCE,
    compute_depth_only_features,
)
from trading.data.cas_depth.normalize import normalize_tbt_depth, sort_levels
from trading.data.cas_depth.quality import DepthQualityTracker


@dataclass
class _FakeDepth:
    """Minimal stand-in for fyers_apiv3 Depth 50-slot arrays."""

    bidprice: list[float] = field(default_factory=lambda: [0.0] * 50)
    askprice: list[float] = field(default_factory=lambda: [0.0] * 50)
    bidqty: list[int] = field(default_factory=lambda: [0] * 50)
    askqty: list[int] = field(default_factory=lambda: [0] * 50)
    bidordn: list[int] = field(default_factory=lambda: [0] * 50)
    askordn: list[int] = field(default_factory=lambda: [0] * 50)
    snapshot: bool = False
    timestamp: int = 1_728_000_000_000
    seqNo: int = 1  # noqa: N815


def _now() -> datetime:
    return datetime(2026, 9, 28, 9, 30, tzinfo=UTC)


def test_sort_levels_dedupes_same_price_keeps_lowest_index() -> None:
    """Stale SDK slots at higher indices must not survive as duplicate prices."""
    bids = [
        DepthLevel(price=Decimal("100.00"), quantity=1000, orders=1),
        DepthLevel(price=Decimal("99.50"), quantity=500, orders=1),
        DepthLevel(price=Decimal("100.00"), quantity=999, orders=9),
    ]
    asks = [
        DepthLevel(price=Decimal("101.00"), quantity=800, orders=2),
        DepthLevel(price=Decimal("101.00"), quantity=700, orders=3),
        DepthLevel(price=Decimal("102.00"), quantity=400, orders=1),
    ]
    sorted_bids, sorted_asks, had_dupes = sort_levels(bids, asks)
    assert had_dupes is True
    assert len(sorted_bids) == 2
    assert len(sorted_asks) == 2
    assert sorted_bids[0].price == Decimal("100.00")
    assert sorted_bids[0].quantity == 1000
    assert sorted_asks[0].price == Decimal("101.00")
    assert sorted_asks[0].quantity == 800


def test_normalize_tbt_depth_dedups_reliance_like_book() -> None:
    """Production RELIANCE ask duplicates collapse to unique sorted levels."""
    depth = _FakeDepth()
    depth.bidprice[0] = 3.50
    depth.bidqty[0] = 5000
    depth.bidprice[1] = 3.25
    depth.bidqty[1] = 1000
    depth.bidprice[2] = 3.00
    depth.bidqty[2] = 500
    ask_pairs = [
        (3.75, 1000),
        (3.75, 1000),
        (4.00, 8500),
        (4.00, 8500),
        (4.10, 1000),
        (4.10, 1000),
    ]
    for index, (price, qty) in enumerate(ask_pairs):
        depth.askprice[index] = price
        depth.askqty[index] = qty
        depth.askordn[index] = 1
    update, had_dupes = normalize_tbt_depth(
        "NSE:RELIANCE26SEP1300CE",
        depth,
        receive_time=_now(),
    )
    assert update is not None
    assert had_dupes is True
    assert len(update.bid_levels) == 3
    assert len(update.ask_levels) == 3
    assert update.ask_levels[0].price == Decimal("3.75")
    assert update.ask_levels[0].quantity == 1000
    assert all(
        update.ask_levels[i].price < update.ask_levels[i + 1].price
        for i in range(len(update.ask_levels) - 1)
    )


def test_duplicate_price_levels_flagged_in_quality_tracker_path() -> None:
    """Repeated price levels before dedup surface DUPLICATE without blocking."""
    depth = _FakeDepth()
    depth.bidprice[0] = 100.0
    depth.bidqty[0] = 1000
    depth.askprice[0] = 101.0
    depth.askqty[0] = 800
    depth.askprice[1] = 101.0
    depth.askqty[1] = 700
    update, had_dupes = normalize_tbt_depth(
        "NFO:NIFTY26SEP22900PE", depth, receive_time=_now()
    )
    assert had_dupes is True
    assert update is not None
    issues: list[DepthQualityIssue] = list(
        DepthQualityTracker(max_stale_seconds=300.0).inspect(update, now=_now())
    )
    if had_dupes:
        issues.append(DepthQualityIssue.DUPLICATE)
    assert DepthQualityIssue.DUPLICATE in issues


def test_top5_imbalance_nonzero_on_asymmetric_deduped_book() -> None:
    """Top-5 imbalance reflects unique price levels on a realistic 50-level book."""
    bids = tuple(
        DepthLevel(
            price=Decimal("100.00") - Decimal(i), quantity=5000 if i < 10 else 100
        )
        for i in range(50)
    )
    asks = tuple(
        DepthLevel(
            price=Decimal("101.00") + Decimal(i), quantity=1000 if i < 10 else 4000
        )
        for i in range(50)
    )
    update = NormalizedDepthUpdate(
        symbol="NFO:NIFTY26SEP22900PE",
        exchange_timestamp=_now(),
        receive_timestamp=_now(),
        sequence=1,
        bid_levels=bids,
        ask_levels=asks,
        trade_aggressor=TradeAggressor.UNKNOWN,
        is_snapshot=True,
        feed="tbt_ws",
    )
    features = compute_depth_only_features(update, None)
    assert features[FEATURE_TOP5_IMBALANCE] != Decimal("0.0000")
    assert features[FEATURE_TOP50_IMBALANCE] != Decimal("0.0000")


def test_duplicate_levels_can_zero_top5_before_dedup() -> None:
    """Duplicate ask slots can falsely balance top5; dedup restores asymmetry."""
    bids = (
        DepthLevel(price=Decimal("100.00"), quantity=2000),
        DepthLevel(price=Decimal("99.50"), quantity=2000),
        DepthLevel(price=Decimal("99.00"), quantity=2000),
    )
    duplicated_asks = (
        DepthLevel(price=Decimal("101.00"), quantity=1000),
        DepthLevel(price=Decimal("101.00"), quantity=1000),
        DepthLevel(price=Decimal("102.00"), quantity=1000),
        DepthLevel(price=Decimal("102.00"), quantity=1000),
        DepthLevel(price=Decimal("103.00"), quantity=2000),
    )
    raw_update = NormalizedDepthUpdate(
        symbol="NFO:NIFTY26SEP22900PE",
        exchange_timestamp=_now(),
        receive_timestamp=_now(),
        sequence=1,
        bid_levels=bids,
        ask_levels=duplicated_asks,
        trade_aggressor=TradeAggressor.UNKNOWN,
        is_snapshot=False,
        feed="tbt_ws",
    )
    deduped_bids, deduped_asks, _ = sort_levels(list(bids), list(duplicated_asks))
    deduped_update = raw_update.model_copy(
        update={"bid_levels": deduped_bids, "ask_levels": deduped_asks},
    )
    raw_features = compute_depth_only_features(raw_update, None)
    deduped_features = compute_depth_only_features(deduped_update, None)
    assert raw_features[FEATURE_TOP5_IMBALANCE] == Decimal("0.0000")
    assert deduped_features[FEATURE_TOP5_IMBALANCE] > Decimal("0.0000")


def test_duplicate_quality_issue_does_not_abstain(tmp_path: Path) -> None:
    """DUPLICATE alone must not force CAS abstain."""
    adapter = CasDepthPaperAdapter(tmp_path / "cas_depth")
    update = NormalizedDepthUpdate(
        symbol="NFO:NIFTY26SEP22900PE",
        exchange_timestamp=_now(),
        receive_timestamp=_now(),
        sequence=1,
        bid_levels=(DepthLevel(price=Decimal("100.00"), quantity=1000),),
        ask_levels=(DepthLevel(price=Decimal("101.00"), quantity=800),),
        trade_aggressor=TradeAggressor.UNKNOWN,
        is_snapshot=True,
        feed="tbt_ws",
    )
    snapshot = adapter.publish(
        update,
        None,
        quality_issues=(DepthQualityIssue.DUPLICATE,),
        snapshot_id="snap-dup",
        now=_now(),
        max_stale_seconds=300.0,
    )
    assert snapshot.abstain is False
    assert adapter.should_abstain() is False


def test_health_json_tracks_every_collected_symbol(tmp_path: Path) -> None:
    """health.json keeps legacy top-level fields and adds per-symbol status."""
    adapter = CasDepthPaperAdapter(tmp_path / "cas_depth")
    now = _now()

    def _publish(symbol: str, bid_qty: int, ask_qty: int) -> None:
        prior_update = NormalizedDepthUpdate(
            symbol=symbol,
            exchange_timestamp=now,
            receive_timestamp=now,
            sequence=1,
            bid_levels=(
                DepthLevel(price=Decimal("100.00"), quantity=bid_qty - 100),
                DepthLevel(price=Decimal("99.50"), quantity=500),
            ),
            ask_levels=(
                DepthLevel(price=Decimal("101.00"), quantity=ask_qty - 50),
                DepthLevel(price=Decimal("101.50"), quantity=400),
            ),
            trade_aggressor=TradeAggressor.UNKNOWN,
            is_snapshot=True,
            feed="tbt_ws",
        )
        update = NormalizedDepthUpdate(
            symbol=symbol,
            exchange_timestamp=now,
            receive_timestamp=now,
            sequence=2,
            bid_levels=(
                DepthLevel(price=Decimal("100.00"), quantity=bid_qty),
                DepthLevel(price=Decimal("99.50"), quantity=500),
            ),
            ask_levels=(
                DepthLevel(price=Decimal("101.00"), quantity=ask_qty),
                DepthLevel(price=Decimal("101.50"), quantity=400),
            ),
            trade_aggressor=TradeAggressor.UNKNOWN,
            is_snapshot=False,
            feed="tbt_ws",
        )
        adapter.publish(
            prior_update,
            None,
            quality_issues=(),
            snapshot_id=f"snap-{symbol}-prior",
            now=now,
            max_stale_seconds=300.0,
        )
        adapter.publish(
            update,
            prior_update,
            quality_issues=(),
            snapshot_id=f"snap-{symbol}",
            now=now,
            max_stale_seconds=300.0,
        )

    _publish("NFO:NIFTY26SEP22900PE", 1000, 800)
    _publish("NSE:RELIANCE26SEP1300CE", 2000, 900)

    health = json.loads(
        (tmp_path / "cas_depth" / "health.json").read_text(encoding="utf-8")
    )
    assert health["cas_data_mode"] == "DEPTH_ONLY"
    assert health["symbol"] == "NSE:RELIANCE26SEP1300CE"
    assert "symbols" in health
    assert set(health["symbols"]) == {
        "NFO_NIFTY26SEP22900PE",
        "NSE_RELIANCE26SEP1300CE",
    }
    assert health["symbols"]["NFO_NIFTY26SEP22900PE"]["features_complete"] is True
    assert health["symbols"]["NSE_RELIANCE26SEP1300CE"]["features_complete"] is True
    assert health["features_complete"] is True
    assert health["abstain"] is False
