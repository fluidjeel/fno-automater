"""CAS-001: Layer 1 produces cas-microstructure-v1 keys from depth events."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import tests.factories as f
from trading.analytics.eligibility import evaluate_eligibility
from trading.analytics.scorecard import build_scorecard
from trading.config import load_evaluation_config
from trading.data.cas_features import (
    CAS_FEATURE_KEYS,
    CAS_FEATURE_SET_VERSION,
    compute_cas_features,
    with_cas_feature_set,
)
from trading.data.config import UnderlyingConfig
from trading.data.events import CanonicalMarketEvent
from trading.data.snapshot_builder import MarketSnapshotBuilder
from trading.domain.enums import (
    AssetClass,
    EligibilityStatus,
    Exchange,
    InstrumentKind,
)
from trading.strategies.cas_microstructure import REQUIRED_FEATURES

NOW = datetime(2026, 9, 14, 9, 40, tzinfo=UTC)


def _depth(
    event_id: str,
    *,
    buy: int,
    sell: int,
    bid: str,
    ask: str,
    bid_vol: int,
    ask_vol: int,
    offset_seconds: int = 0,
) -> CanonicalMarketEvent:
    instant = NOW + timedelta(seconds=offset_seconds)
    return CanonicalMarketEvent(
        event_id=event_id,
        provider="fyers",
        symbol="NSE:NIFTY50-INDEX",
        event_type="DEPTH_SNAPSHOT",
        event_time=instant,
        source_time=instant,
        receive_time=instant,
        provider_sequence=None,
        payload={
            "bid_levels": [{"price": bid, "volume": bid_vol}],
            "ask_levels": [{"price": ask, "volume": ask_vol}],
            "total_buy_qty": buy,
            "total_sell_qty": sell,
        },
        raw_ref=f"raw://{event_id}",
        normalization_version="1",
    )


def test_feature_keys_match_the_strategy_contract() -> None:
    assert REQUIRED_FEATURES == CAS_FEATURE_KEYS
    assert CAS_FEATURE_SET_VERSION == "cas-microstructure-v1"


def test_single_depth_omits_flow_and_instability() -> None:
    features = compute_cas_features(
        (
            _depth(
                "d1",
                buy=150,
                sell=120,
                bid="24500",
                ask="24501",
                bid_vol=150,
                ask_vol=120,
            ),
        )
    )
    assert "cas_auction_imbalance" in features
    assert "cas_microprice_edge_bps" in features
    assert "cas_trade_flow_imbalance" not in features
    assert "cas_quote_instability" not in features
    assert with_cas_feature_set(f.snapshot(features=features)) is None


def test_two_depths_complete_the_feature_set() -> None:
    first = _depth(
        "d1", buy=100, sell=100, bid="24500", ask="24501", bid_vol=100, ask_vol=100
    )
    second = _depth(
        "d2",
        buy=180,
        sell=80,
        bid="24500.5",
        ask="24501.5",
        bid_vol=180,
        ask_vol=80,
        offset_seconds=5,
    )
    features = compute_cas_features((first, second))
    assert set(CAS_FEATURE_KEYS) <= set(features)
    assert features["cas_auction_imbalance"] > 0
    stamped = with_cas_feature_set(f.snapshot(features=features))
    assert stamped is not None
    assert stamped.feature_set_version == CAS_FEATURE_SET_VERSION


def test_missing_depth_yields_no_defaults() -> None:
    assert compute_cas_features(()) == {}


def test_snapshot_builder_stamps_cas_version_when_keys_complete() -> None:
    builder = MarketSnapshotBuilder(
        UnderlyingConfig(
            symbol="NSE:NIFTY50-INDEX",
            feature_set_version="nifty_index_v1",
            exchange=Exchange.NSE,
            underlying="NIFTY",
            instrument_kind=InstrumentKind.INDEX,
            asset_class=AssetClass.EQUITY_INDEX,
        ),
        config_version="1",
        config_checksum="abc",
        code_version="0.1.0",
    )
    chain = CanonicalMarketEvent(
        event_id="c1",
        provider="fyers",
        symbol="NSE:NIFTY50-INDEX",
        event_type="OPTION_CHAIN_SNAPSHOT",
        event_time=NOW,
        source_time=NOW,
        receive_time=NOW,
        provider_sequence=None,
        payload={"strikes": [{"option_type": "", "ltp": "24500"}]},
        raw_ref="raw://c1",
        normalization_version="1",
    )
    first = _depth(
        "d1", buy=100, sell=100, bid="24500", ask="24501", bid_vol=100, ask_vol=100
    )
    second = _depth(
        "d2",
        buy=180,
        sell=80,
        bid="24500.5",
        ask="24501.5",
        bid_vol=180,
        ask_vol=80,
        offset_seconds=5,
    )
    snapshot = builder.build(
        (chain, first, second),
        as_of=NOW + timedelta(seconds=5),
        quality=f.quality(),
    )
    assert snapshot.feature_set_version == CAS_FEATURE_SET_VERSION
    assert set(CAS_FEATURE_KEYS) <= set(snapshot.features)


def test_cas_shadow_cohort_is_ineligible_until_charges_are_verified() -> None:
    package = f.long_option_cohort_package(
        experiment=f.experiment(
            strategy_id="cas_microstructure",
            strategy_version="cas-microstructure-v1",
            feature_set_version=CAS_FEATURE_SET_VERSION,
        )
    )
    loaded = load_evaluation_config(
        Path(__file__).resolve().parent.parent / "config" / "evaluation.yaml"
    )
    scorecard = build_scorecard(
        package, loaded.config.fill_model, as_of=package.observation_end
    )
    result = evaluate_eligibility(
        scorecard,
        loaded.config.eligibility,
        evaluated_at=package.observation_end,
        threshold_checksum=loaded.checksum,
    )
    assert result.status is not EligibilityStatus.ELIGIBLE
