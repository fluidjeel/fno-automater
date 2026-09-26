"""DISC-A5: staleness measured from quote time, not bar event time."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

import tests.factories as f
from trading.config import load_config
from trading.config.discovery import load_discovery_config
from trading.domain.contracts import (
    DerivativesContext,
    FeatureSnapshot,
    Greeks,
    SnapshotTimes,
)
from trading.domain.enums import EntryProfile, OptionType, ReasonCode
from trading.strategies.base import StrategyContext
from trading.strategies.m4_broad_basket import LongStraddleStrategy
from trading.strategies.macro import MacroAssessment, MacroBias
from trading.strategies.quote_freshness import quote_freshness_limits

REPO_ROOT = Path(__file__).resolve().parent.parent
DISCOVERY = load_discovery_config(REPO_ROOT / "config" / "discovery.yaml")
ACCOUNT_CONFIG = load_config(REPO_ROOT / "config" / "base.yaml")
NOW = datetime.fromisoformat("2026-09-22T05:00:00+00:00")
EXPIRY = date(2026, 10, 1)


def _macro() -> MacroAssessment:
    return MacroAssessment(
        regime="NEUTRAL_VOLATILITY",
        directional_bias=MacroBias.NEUTRAL,
        confidence=Decimal("0.8"),
        fresh_until=NOW + timedelta(hours=1),
        evidence_ids=("src-1",),
        model_version="macro-v1",
    )


def _times(
    now: datetime,
    *,
    quote_age_seconds: int,
    bar_age_seconds: int,
) -> SnapshotTimes:
    quote_at = now - timedelta(seconds=quote_age_seconds)
    bar_at = now - timedelta(seconds=bar_age_seconds)
    event_at = min(bar_at, quote_at) - timedelta(milliseconds=100)
    receive_at = quote_at - timedelta(milliseconds=50)
    if receive_at < event_at:
        receive_at = event_at + timedelta(milliseconds=50)
    return f.snapshot_times(
        event_time=event_at,
        source_time=event_at,
        receive_time=receive_at,
        calculation_time=quote_at,
    )


def _option(symbol: str, *, strike: str, times: SnapshotTimes) -> FeatureSnapshot:
    return f.snapshot(
        contract=f.option_contract(
            symbol=symbol,
            strike=Decimal(strike),
            option_type=OptionType.CALL
            if symbol.endswith("CE")
            else OptionType.PUT,
            expiry=EXPIRY,
        ),
        times=times,
        market=f.quote(
            bid=f.price("90.00"),
            ask=f.price("90.10"),
            last=f.price("90.10"),
            bid_size=500,
            ask_size=500,
        ),
        features={"lot_size": Decimal(65), "top_of_book_observed": Decimal(1)},
        derivatives=DerivativesContext(
            days_to_expiry=10,
            open_interest=5000,
            option_type=OptionType.CALL
            if symbol.endswith("CE")
            else OptionType.PUT,
            greeks=Greeks(
                model="fixture",
                calculation_version="1",
                converged=True,
                implied_volatility=Decimal("15"),
                delta=Decimal("0.50"),
            ),
            underlying_price=f.price("24500"),
        ),
    )


def _straddle_candidates(times: SnapshotTimes) -> tuple[FeatureSnapshot, ...]:
    return (
        _option("NIFTY26OCT24500PE", strike="24500", times=times),
        _option("NIFTY26OCT24500CE", strike="24500", times=times),
    )


def _ctx(
    *,
    quote_age_seconds: int,
    bar_age_seconds: int,
    entry_profile: EntryProfile,
) -> StrategyContext:
    now = NOW
    times = _times(
        now,
        quote_age_seconds=quote_age_seconds,
        bar_age_seconds=bar_age_seconds,
    )
    strict_ms, hard_ms = quote_freshness_limits(
        entry_profile=entry_profile,
        freshness=ACCOUNT_CONFIG.config.freshness,
        discovery_config=DISCOVERY if entry_profile is EntryProfile.DISCOVERY else None,
        cas=False,
    )
    return StrategyContext(
        underlying=f.snapshot(
            snapshot_id="SNAP-UNDER",
            contract=f.index_contract(),
            times=times,
            market=f.quote(last=f.price("24500"), close=f.price("24500")),
        ),
        candidates=_straddle_candidates(times),
        view=f.portfolio_view(),
        now=now,
        macro=_macro(),
        entry_profile=entry_profile,
        strict_quote_max_age_ms=strict_ms,
        hard_quote_max_age_ms=hard_ms,
    )


class TestDiscA5QuoteFreshness:
    def test_fresh_quote_stale_bar_does_not_data_stale_strict(self) -> None:
        """Quote 20s old with a 4m bar must not reject DATA_STALE under STRICT."""
        decision = LongStraddleStrategy().evaluate(
            _ctx(
                quote_age_seconds=20,
                bar_age_seconds=240,
                entry_profile=EntryProfile.STRICT,
            )
        )
        assert decision.emits_intent
        assert not any(
            rejection.reason is ReasonCode.DATA_STALE for rejection in decision.rejections
        )

    def test_fresh_quote_stale_bar_does_not_data_stale_discovery(self) -> None:
        """Quote 20s old with a 4m bar must not reject DATA_STALE under DISCOVERY."""
        decision = LongStraddleStrategy().evaluate(
            _ctx(
                quote_age_seconds=20,
                bar_age_seconds=240,
                entry_profile=EntryProfile.DISCOVERY,
            )
        )
        assert decision.emits_intent
        assert not any(
            rejection.reason is ReasonCode.DATA_STALE for rejection in decision.rejections
        )

    def test_three_minute_quote_is_soft_stale_in_discovery(self) -> None:
        decision = LongStraddleStrategy().evaluate(
            _ctx(
                quote_age_seconds=180,
                bar_age_seconds=180,
                entry_profile=EntryProfile.DISCOVERY,
            )
        )
        assert decision.emits_intent
        assert ReasonCode.DATA_STALE in decision.strict_would_block
        assert not any(
            rejection.reason is ReasonCode.DATA_STALE for rejection in decision.rejections
        )

    def test_three_minute_quote_rejects_under_strict(self) -> None:
        decision = LongStraddleStrategy().evaluate(
            _ctx(
                quote_age_seconds=180,
                bar_age_seconds=180,
                entry_profile=EntryProfile.STRICT,
            )
        )
        assert not decision.emits_intent
        assert decision.rejections[0].reason is ReasonCode.DATA_STALE

    @pytest.mark.parametrize("profile", [EntryProfile.STRICT, EntryProfile.DISCOVERY])
    def test_six_minute_quote_hard_rejects(self, profile: EntryProfile) -> None:
        decision = LongStraddleStrategy().evaluate(
            _ctx(
                quote_age_seconds=360,
                bar_age_seconds=360,
                entry_profile=profile,
            )
        )
        assert not decision.emits_intent
        assert decision.rejections[0].reason is ReasonCode.DATA_STALE
