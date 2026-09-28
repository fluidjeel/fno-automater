"""DISC-A18: rate-limit-aware promoted-strike WebSocket depth."""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from tests import factories as f
from trading.config.depth_promotion import (
    DepthPromotionConfig,
    load_depth_promotion_config,
)
from trading.data.depth_promotion import DepthSetPromoter, score_chain_strikes
from trading.data.events import CanonicalMarketEvent, RawMarketCapture
from trading.data.fyers.client import FyersApiError
from trading.data.paper_chain_cache import CachedOptionChainFeed, is_fyers_rate_limited
from trading.data.paper_option_depth import attach_promoted_depth
from trading.data.promoted_depth_ws import IncrementalDepthSubscriptions
from trading.domain.clock import FrozenClock
from trading.domain.contracts import FeatureSnapshot
from trading.domain.contracts.snapshot import DerivativesContext
from trading.domain.enums import ChainFetchMode, OptionType

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 28, 5, 0, tzinfo=UTC)


def _depth_config(**overrides: object) -> DepthPromotionConfig:
    base = load_depth_promotion_config(ROOT / "config" / "paper_data.yaml")
    if not overrides:
        return base
    payload = base.model_dump(mode="json")
    payload.update(overrides)
    return DepthPromotionConfig.model_validate(payload)


def _chain_event(
    rows: list[dict[str, object]],
    *,
    symbol: str = "NSE:NIFTY50-INDEX",
) -> CanonicalMarketEvent:
    return CanonicalMarketEvent(
        event_id="chain-test",
        provider="fyers",
        symbol=symbol,
        event_type="OPTION_CHAIN_SNAPSHOT",
        event_time=NOW,
        source_time=NOW,
        receive_time=NOW,
        provider_sequence=None,
        payload={"strikes": rows},
        raw_ref="test",
        normalization_version="1",
    )


def _option_row(
    symbol: str,
    strike: int,
    *,
    oi: int = 1000,
    volume: int = 100,
    option_type: str = "CE",
) -> dict[str, object]:
    return {
        "symbol": symbol,
        "strike_price": strike,
        "option_type": option_type,
        "oi": oi,
        "volume": volume,
        "ltp": 100,
        "bid": 99,
        "ask": 101,
    }


def _option_snapshot(symbol: str) -> FeatureSnapshot:
    return f.snapshot(
        contract=f.option_contract(symbol=symbol),
        derivatives=DerivativesContext(
            days_to_expiry=10,
            open_interest=5000,
            option_type=OptionType.CALL,
            underlying_price=f.price("25000"),
        ),
    )


class _FakeChainFeed:
    def __init__(self, *, fail_times: int = 0) -> None:
        self.calls = 0
        self._fail_times = fail_times

    def fetch_option_chain(
        self, symbol: str, *, expiry_epoch: int | None = None
    ) -> RawMarketCapture:
        self.calls += 1
        if self._fail_times > 0:
            self._fail_times -= 1
            raise FyersApiError("Fyers error 429: rate limit")
        return RawMarketCapture(
            capture_id=f"cap-{self.calls}",
            provider="fyers",
            endpoint="options-chain-v3",
            received_at=NOW,
            payload={"s": "ok", "data": {"optionsChain": []}},
            http_status=200,
        )


class _FakeDepthFeed:
    def __init__(self) -> None:
        self.depth_calls: list[str] = []

    def fetch_depth(self, symbol: str) -> RawMarketCapture:
        self.depth_calls.append(symbol)
        raise AssertionError("REST depth should not be called in these tests")


def test_depth_promotion_config_round_trips() -> None:
    """Config contract survives JSON round-trip."""
    loaded = load_depth_promotion_config(ROOT / "config" / "paper_data.yaml")
    assert loaded.round_trip() == loaded
    assert loaded.depth_set_size == 8
    assert loaded.promote_after_polls == 2
    assert loaded.demote_after_polls == 3


def test_promotion_requires_consecutive_polls() -> None:
    """A strike promotes only after promote_after_polls in the top set."""
    config = _depth_config(promote_after_polls=2, demote_after_polls=3)
    promoter = DepthSetPromoter(config)
    rows = [
        {"option_type": "", "ltp": 25000},
        _option_row("NSE:NIFTY24SEP25000CE", 25000, oi=5000),
        _option_row("NSE:NIFTY24SEP25100CE", 25100, oi=1000),
    ]
    chain = _chain_event(rows)
    assert promoter.poll(chain) == frozenset()
    promoted = promoter.poll(chain)
    assert "NSE:NIFTY24SEP25000CE" in promoted


def test_demotion_requires_consecutive_polls() -> None:
    """A promoted strike demotes only after demote_after_polls outside the top set."""
    config = _depth_config(
        promote_after_polls=1, demote_after_polls=3, depth_set_size=5
    )
    promoter = DepthSetPromoter(config)
    hot = _option_row("NSE:NIFTY24SEP25000CE", 25000, oi=9000)
    cold = _option_row("NSE:NIFTY24SEP25100CE", 25100, oi=100)
    fillers = [
        _option_row(f"NSE:NIFTY24SEP{strike}CE", strike, oi=500)
        for strike in (24900, 24950, 25050, 25150, 25200)
    ]
    chain_hot = _chain_event([{"option_type": "", "ltp": 25000}, hot, *fillers])
    chain_cold = _chain_event([{"option_type": "", "ltp": 25000}, cold, *fillers])
    promoted_hot = promoter.poll(chain_hot)
    assert "NSE:NIFTY24SEP25000CE" in promoted_hot
    for _ in range(2):
        assert "NSE:NIFTY24SEP25000CE" in promoter.poll(chain_cold)
    demoted = promoter.poll(chain_cold)
    assert "NSE:NIFTY24SEP25000CE" not in demoted


def test_ws_subscription_matches_promoted_set() -> None:
    """Subscription set equals promoted strikes after incremental deltas."""
    config = _depth_config(promote_after_polls=1, depth_set_size=5)
    promoter = DepthSetPromoter(config)
    subscriptions = IncrementalDepthSubscriptions()
    rows = [
        {"option_type": "", "ltp": 25000},
        _option_row("NSE:NIFTY24SEP24900CE", 24900, oi=3000),
        _option_row("NSE:NIFTY24SEP25000CE", 25000, oi=5000),
        _option_row("NSE:NIFTY24SEP25100CE", 25100, oi=4000),
        _option_row("NSE:NIFTY24SEP25200CE", 25200, oi=3500),
        _option_row("NSE:NIFTY24SEP25300CE", 25300, oi=3200),
    ]
    chain = _chain_event(rows)
    promoted = promoter.poll(chain)
    subscriptions.apply_delta(promoter.state.last_added, promoter.state.last_removed)
    assert frozenset(subscriptions.subscribed) == promoted
    assert len(subscriptions.subscribed) <= config.depth_set_size


def test_incremental_subscription_deltas_without_wide_subscribe() -> None:
    """Only subscribe/unsubscribe deltas are applied; count never exceeds depth_set_size."""
    config = _depth_config(promote_after_polls=1, depth_set_size=5)
    promoter = DepthSetPromoter(config)
    subscriptions = IncrementalDepthSubscriptions()
    rows = [
        {"option_type": "", "ltp": 25000},
        _option_row("NSE:NIFTY24SEP24900CE", 24900, oi=3000),
        _option_row("NSE:NIFTY24SEP25000CE", 25000, oi=5000),
        _option_row("NSE:NIFTY24SEP25100CE", 25100, oi=4000),
        _option_row("NSE:NIFTY24SEP25200CE", 25200, oi=3500),
        _option_row("NSE:NIFTY24SEP25300CE", 25300, oi=3200),
    ]
    chain = _chain_event(rows)
    promoted = promoter.poll(chain)
    subscriptions.apply_delta(promoter.state.last_added, promoter.state.last_removed)
    assert len(subscriptions.subscribed) == len(promoted)
    assert len(subscriptions.subscribed) <= 5
    assert subscriptions.actions[-1].kind == "subscribe"
    wide = [
        _option_row(f"NSE:NIFTY24SEP{strike}CE", strike, oi=strike)
        for strike in range(24000, 26000, 50)
    ]
    chain_wide = _chain_event([{"option_type": "", "ltp": 25000}, *wide])
    score_chain_strikes(chain_wide, Decimal("25000"), config)
    promoted_again = promoter.poll(chain_wide)
    subscriptions.apply_delta(promoter.state.last_added, promoter.state.last_removed)
    assert len(subscriptions.subscribed) <= config.depth_set_size
    assert len(subscriptions.subscribed) == len(promoted_again)


def test_429_backoff_serves_from_cache_without_crash() -> None:
    """429 backoff serves cached chain and records health telemetry."""
    clock = FrozenClock(NOW)
    config = _depth_config(chain_cache_ttl_seconds=120)
    feed = _FakeChainFeed()
    cache = CachedOptionChainFeed(
        feed, config, clock, jitter_rng=__import__("random").Random(0)
    )
    first, health = cache.fetch_option_chain("NSE:NIFTY50-INDEX", now=NOW)
    assert health.fetch_mode is ChainFetchMode.LIVE
    assert feed.calls == 1
    second, cached_health = cache.fetch_option_chain(
        "NSE:NIFTY50-INDEX",
        now=NOW + timedelta(seconds=30),
    )
    assert cached_health.fetch_mode is ChainFetchMode.CACHED
    assert second.capture_id == first.capture_id
    assert feed.calls == 1
    feed._fail_times = 1
    third, backoff_health = cache.fetch_option_chain(
        "NSE:NIFTY50-INDEX",
        now=NOW + timedelta(seconds=130),
    )
    assert is_fyers_rate_limited(FyersApiError("Fyers error 429: limit"))
    assert backoff_health.fetch_mode is ChainFetchMode.BACKOFF
    assert backoff_health.rate_limit_429_count == 1
    assert third.capture_id == first.capture_id


def test_unpromoted_strikes_receive_no_depth() -> None:
    """Only promoted strikes receive depth attachment attempts."""
    config = _depth_config()
    candidates = (
        _option_snapshot("NSE:NIFTY24SEP25000CE"),
        _option_snapshot("NSE:NIFTY24SEP25100CE"),
    )
    health = CachedOptionChainFeed(
        _FakeChainFeed(), config, FrozenClock(NOW)
    ).fetch_option_chain(
        "NSE:NIFTY50-INDEX",
        now=NOW,
    )[1]
    updated, attachment = attach_promoted_depth(
        candidates,
        promoted=frozenset({"NSE:NIFTY24SEP25000CE"}),
        depth_ws=None,
        feed=_FakeDepthFeed(),
        config=config,
        chain_health=health,
    )
    assert "NSE:NIFTY24SEP25100CE" not in attachment.sources
    assert updated[0].market.bid_size is None
    assert updated[1].market.bid_size is None


def test_new_modules_do_not_reference_cas_depth() -> None:
    """CAS depth collector stays a separate process and is not imported."""
    modules = [
        ROOT / "src/trading/config/depth_promotion.py",
        ROOT / "src/trading/data/paper_chain_cache.py",
        ROOT / "src/trading/data/depth_promotion.py",
        ROOT / "src/trading/data/promoted_depth_ws.py",
        ROOT / "src/trading/data/paper_option_depth.py",
        ROOT / "src/trading/data/paper_market_stack.py",
    ]
    for path in modules:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert "cas_depth" not in alias.name
            if isinstance(node, ast.ImportFrom) and node.module:
                assert "cas_depth" not in node.module
