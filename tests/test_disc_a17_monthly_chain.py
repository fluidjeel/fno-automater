"""DISC-A17: monthly-window supplemental chain fetch for M3/M4 binders."""

from __future__ import annotations

import time
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

import tests.factories as f
from trading.data.events import CanonicalMarketEvent
from trading.data.fyers.client import FyersApiError
from trading.data.storage.instrument_store import InstrumentSpecStore
from trading.domain.contracts import DerivativesContext, FeatureSnapshot, MarketState
from trading.domain.contracts.identification import (
    MacroStatus,
    TrendState,
    VolatilityState,
)
from trading.domain.contracts.snapshot import Greeks
from trading.domain.enums import DataQuality, ModeId, OptionType
from trading.identification import bind_debit_spread, load_identification_policy
from trading.identification.calendar import get_calendar_port
from trading.runtime.four_mode_producers import _candidates_for_mode
from trading.runtime.m2_chain import monthly_window_epoch
from trading.runtime.paper_session import _merge_monthly_chain
from trading.strategies.cas_microstructure import MIN_DAYS_TO_EXPIRY

ROOT = Path(__file__).resolve().parent.parent
IST = ZoneInfo("Asia/Kolkata")
POLICY = load_identification_policy(ROOT / "config" / "identification.yaml")


def _market(
    trend: TrendState = TrendState.UP,
    calculated_at: datetime | None = None,
) -> MarketState:
    at = calculated_at or datetime(2026, 9, 28, 5, 0, tzinfo=UTC)
    return MarketState.model_validate(
        {
            "market_state_id": "market-a17",
            "feature_version": POLICY.feature_version,
            "calculated_at": at.astimezone(UTC),
            "source_snapshot_ids": ("snapshot-1",),
            "trend": trend,
            "volatility": VolatilityState.NORMAL,
            "return_15m": Decimal("0.004"),
            "return_60m": Decimal("0.012"),
            "normalized_return_15m": Decimal("0.7"),
            "normalized_return_60m": Decimal("0.8"),
            "normalized_vwap_distance": Decimal("0.5"),
            "realized_volatility_ratio": Decimal("1.0"),
            "realized_volatility_annualized": Decimal("15"),
            "iv_percentile": Decimal("30"),
            "iv_rv_ratio": Decimal("1.0"),
            "trend_score": Decimal("0.7"),
            "event_state": "NORMAL",
            "macro_status": MacroStatus.NEUTRAL,
            "quality": DataQuality.VALID,
            "warmup_complete": True,
            "completed_bar_count": 60,
            "session_count": 20,
            "reason_codes": (),
        }
    )


def _chain_payload() -> dict[str, object]:
    return {
        "expiry_data": [
            {"date": "29-09-2026", "expiry": "1790676600"},
            {"date": "06-10-2026", "expiry": "1791281400"},
            {"date": "13-10-2026", "expiry": "1791886200"},
            {"date": "19-10-2026", "expiry": "1792491000"},
            {"date": "27-10-2026", "expiry": "1793182200"},
        ],
        "strikes": [],
    }


def _chain() -> CanonicalMarketEvent:
    return CanonicalMarketEvent(
        event_id="evt-1",
        event_type="OPTION_CHAIN_SNAPSHOT",
        symbol="NSE:NIFTY50-INDEX",
        event_time=datetime(2026, 9, 28, 4, 0, tzinfo=UTC),
        receive_time=datetime(2026, 9, 28, 4, 0, tzinfo=UTC),
        source_time=datetime(2026, 9, 28, 4, 0, tzinfo=UTC),
        provider="fyers",
        provider_sequence=1,
        normalization_version="1",
        raw_ref="test",
        payload=_chain_payload(),
    )


def _monthly_candidate(*, dte: int = 29) -> tuple[FeatureSnapshot, ...]:
    expiry = date(2026, 10, 27)
    long_leg = f.snapshot(
        contract=f.option_contract(
            symbol="NIFTY26OCT24000CE",
            expiry=expiry,
            strike=Decimal("24000"),
            option_type=OptionType.CALL,
        ),
        market=f.quote(
            bid=f.price("100"),
            ask=f.price("101"),
            last=f.price("100"),
        ),
        derivatives=DerivativesContext(
            days_to_expiry=dte,
            open_interest=5000,
            option_type=OptionType.CALL,
            greeks=Greeks(
                model="fixture",
                calculation_version="1",
                converged=True,
                implied_volatility=Decimal("15"),
                delta=Decimal("0.50"),
            ),
            underlying_price=f.price("24000"),
        ),
        features={"lot_size": Decimal(75), "top_of_book_observed": Decimal(1)},
    )
    short_leg = f.snapshot(
        contract=f.option_contract(
            symbol="NIFTY26OCT24500CE",
            expiry=expiry,
            strike=Decimal("24500"),
            option_type=OptionType.CALL,
        ),
        market=f.quote(
            bid=f.price("80"),
            ask=f.price("81"),
            last=f.price("80"),
        ),
        derivatives=DerivativesContext(
            days_to_expiry=dte,
            open_interest=5000,
            option_type=OptionType.CALL,
            greeks=Greeks(
                model="fixture",
                calculation_version="1",
                converged=True,
                implied_volatility=Decimal("15"),
                delta=Decimal("0.25"),
            ),
            underlying_price=f.price("24000"),
        ),
        features={"lot_size": Decimal(75), "top_of_book_observed": Decimal(1)},
    )
    return (long_leg, short_leg)


def test_monthly_window_epoch_prefers_true_monthly_in_window() -> None:
    """Monthly selection picks last listed monthly expiry in 20-35 DTE window."""
    epoch = monthly_window_epoch(
        _chain(),
        as_of=date(2026, 9, 28),
        calendar=get_calendar_port(),
        already_listed={date(2026, 9, 29), date(2026, 10, 6)},
        monthly_dte_min=POLICY.contracts.monthly_dte_min,
        monthly_dte_max=POLICY.contracts.monthly_dte_max,
    )
    assert epoch == 1793182200


def test_monthly_window_epoch_returns_none_when_already_loaded() -> None:
    """Skip monthly fetch when the in-window expiry is already merged."""
    epoch = monthly_window_epoch(
        _chain(),
        as_of=date(2026, 9, 28),
        calendar=get_calendar_port(),
        already_listed={date(2026, 10, 27)},
        monthly_dte_min=POLICY.contracts.monthly_dte_min,
        monthly_dte_max=POLICY.contracts.monthly_dte_max,
    )
    assert epoch is None


def test_monthly_window_epoch_returns_none_when_no_in_window_expiry() -> None:
    """Skip monthly fetch when no listed expiry falls in the monthly DTE band."""
    payload = {
        "expiry_data": [
            {"date": "29-09-2026", "expiry": "1790676600"},
            {"date": "06-10-2026", "expiry": "1791281400"},
        ],
        "strikes": [],
    }
    chain = replace(_chain(), payload=payload)
    epoch = monthly_window_epoch(
        chain,
        as_of=date(2026, 9, 28),
        calendar=get_calendar_port(),
        already_listed=set(),
        monthly_dte_min=POLICY.contracts.monthly_dte_min,
        monthly_dte_max=POLICY.contracts.monthly_dte_max,
    )
    assert epoch is None


@patch("trading.runtime.paper_session.normalize_fyers_option_chain")
@patch("trading.runtime.paper_session.build_option_candidates")
def test_merge_monthly_chain_marks_candidates(
    build_candidates: object,
    normalize_chain: object,
    tmp_path: Path,
) -> None:
    """Monthly merge tags supplemental rows with monthly_chain=1."""
    monthly_rows = _monthly_candidate()
    build_candidates.return_value = (monthly_rows, {"NIFTY26OCT24000CE": object()})  # type: ignore[attr-defined]
    normalize_chain.return_value = _chain()  # type: ignore[attr-defined]

    class _Feed:
        def fetch_option_chain(
            self, symbol: str, *, expiry_epoch: int | None = None
        ) -> object:
            assert expiry_epoch == 1793182200
            return {"symbol": symbol, "expiry_epoch": expiry_epoch}

    near = f.snapshot(
        contract=f.option_contract(symbol="NIFTY26SEP24000CE", strike=Decimal("24000")),
        derivatives=DerivativesContext(
            days_to_expiry=1,
            open_interest=5000,
            option_type=OptionType.CALL,
            underlying_price=f.price("24000"),
        ),
    )
    merged, specs, error, cache = _merge_monthly_chain(
        (near,),
        {},
        chain=_chain(),
        catalog=InstrumentSpecStore(tmp_path / "instruments"),
        underlying=f.snapshot(contract=f.index_contract()),
        as_of=datetime(2026, 9, 28, 4, 0, tzinfo=UTC),
        zone=IST,
        feed=_Feed(),  # type: ignore[arg-type]
        pipeline_symbol="NSE:NIFTY50-INDEX",
        monthly_dte_min=POLICY.contracts.monthly_dte_min,
        monthly_dte_max=POLICY.contracts.monthly_dte_max,
    )
    assert error is None
    assert cache is not None
    assert len(merged) == 3
    monthly = merged[1:]
    assert all(item.features.get("monthly_chain") == Decimal(1) for item in monthly)
    assert all(
        item.features.get("following_week_chain", Decimal(0)) != Decimal(1)
        for item in monthly
    )
    assert specs


def test_merge_monthly_chain_fetch_failure_keeps_existing_candidates(
    tmp_path: Path,
) -> None:
    """Monthly fetch failure logs detail and retains near/following-week rows."""
    near = f.snapshot(
        contract=f.option_contract(symbol="NIFTY26SEP24000CE", strike=Decimal("24000")),
        derivatives=DerivativesContext(
            days_to_expiry=1,
            open_interest=5000,
            option_type=OptionType.CALL,
            underlying_price=f.price("24000"),
        ),
    )

    class _FailingFeed:
        def fetch_option_chain(
            self, symbol: str, *, expiry_epoch: int | None = None
        ) -> object:
            raise FyersApiError("Fyers error 429: rate limit")

    merged, specs, error, cache = _merge_monthly_chain(
        (near,),
        {},
        chain=_chain(),
        catalog=InstrumentSpecStore(tmp_path / "instruments"),
        underlying=f.snapshot(contract=f.index_contract()),
        as_of=datetime(2026, 9, 28, 4, 0, tzinfo=UTC),
        zone=IST,
        feed=_FailingFeed(),  # type: ignore[arg-type]
        pipeline_symbol="NSE:NIFTY50-INDEX",
        monthly_dte_min=POLICY.contracts.monthly_dte_min,
        monthly_dte_max=POLICY.contracts.monthly_dte_max,
    )
    assert merged == (near,)
    assert error is not None
    assert "monthly chain fetch failed" in error
    assert "429" in error
    assert cache is None
    assert specs == {}


@patch("trading.runtime.paper_session.normalize_fyers_option_chain")
@patch("trading.runtime.paper_session.build_option_candidates")
def test_merge_monthly_chain_cache_avoids_refetch_within_ttl(
    build_candidates: object,
    normalize_chain: object,
    tmp_path: Path,
) -> None:
    """TTL cache serves supplemental rows without a second Fyers REST call."""
    monthly_rows = _monthly_candidate()
    build_candidates.return_value = (monthly_rows, {})  # type: ignore[attr-defined]
    normalize_chain.return_value = _chain()  # type: ignore[attr-defined]
    fetch_calls = 0

    class _Feed:
        def fetch_option_chain(
            self, symbol: str, *, expiry_epoch: int | None = None
        ) -> object:
            nonlocal fetch_calls
            fetch_calls += 1
            return {"symbol": symbol, "expiry_epoch": expiry_epoch}

    feed = _Feed()
    catalog = InstrumentSpecStore(tmp_path / "instruments")
    underlying = f.snapshot(contract=f.index_contract())
    as_of = datetime(2026, 9, 28, 4, 0, tzinfo=UTC)
    near = f.snapshot(
        contract=f.option_contract(symbol="NIFTY26SEP24000CE", strike=Decimal("24000")),
        derivatives=DerivativesContext(
            days_to_expiry=1,
            open_interest=5000,
            option_type=OptionType.CALL,
            underlying_price=f.price("24000"),
        ),
    )
    common = dict(
        chain=_chain(),
        catalog=catalog,
        underlying=underlying,
        as_of=as_of,
        zone=IST,
        feed=feed,
        pipeline_symbol="NSE:NIFTY50-INDEX",
        monthly_dte_min=POLICY.contracts.monthly_dte_min,
        monthly_dte_max=POLICY.contracts.monthly_dte_max,
        cache_ttl_seconds=120,
    )
    merged1, _, _, cache = _merge_monthly_chain((near,), {}, **common)  # type: ignore[arg-type]
    merged2, _, _, cache2 = _merge_monthly_chain(
        (near,),
        {},
        cache=cache,
        **common,  # type: ignore[arg-type]
    )
    assert fetch_calls == 1
    assert cache2 is cache
    assert len(merged1) == len(merged2) == 3
    assert all(item.features.get("monthly_chain") == Decimal(1) for item in merged1[1:])


def test_binder_selects_monthly_window_candidate() -> None:
    """M3 debit spread binder accepts 20-35 DTE monthly-chain candidates."""
    long_leg, short_leg = _monthly_candidate()
    marked = tuple(
        item.model_copy(
            update={"features": {**item.features, "monthly_chain": Decimal(1)}}
        )
        for item in (long_leg, short_leg)
    )
    market = _market(trend=TrendState.UP)
    bound = bind_debit_spread(marked, market=market, policy=POLICY)
    assert bound.binding.eligible is True
    assert set(bound.binding.selected_symbols) == {
        "NIFTY26OCT24000CE",
        "NIFTY26OCT24500CE",
    }


def test_monthly_candidates_filtered_for_m1_and_m2_only() -> None:
    """Monthly rows stay available to M3/M4 but not M1 CAS or M2 expiry pickers."""
    base = f.snapshot(
        contract=f.option_contract(symbol="NIFTY26OCT24000CE", strike=Decimal("24000")),
        derivatives=DerivativesContext(
            days_to_expiry=29,
            open_interest=5000,
            option_type=OptionType.CALL,
            underlying_price=f.price("24000"),
        ),
    )
    marked = base.model_copy(
        update={"features": {**base.features, "monthly_chain": Decimal(1)}}
    )
    assert _candidates_for_mode((marked,), mode_id=ModeId.M1_CAS) == ()
    assert _candidates_for_mode((marked,), mode_id=ModeId.M2_DIRECTIONAL) == ()
    assert _candidates_for_mode((marked,), mode_id=ModeId.M3_TACTICAL_POSITIONAL) == (
        marked,
    )


def test_cas_min_days_to_expiry_unchanged() -> None:
    """CAS MIN_DAYS_TO_EXPIRY remains 1; monthly merge does not alter CAS config."""
    assert MIN_DAYS_TO_EXPIRY == 1


def test_monthly_cache_expires_after_ttl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cache miss after TTL triggers a fresh Fyers fetch."""
    monthly_rows = _monthly_candidate()
    fetch_calls = 0
    monotonic_values = iter([100.0, 100.0, 250.0])

    class _Feed:
        def fetch_option_chain(
            self, symbol: str, *, expiry_epoch: int | None = None
        ) -> object:
            nonlocal fetch_calls
            fetch_calls += 1
            return {"symbol": symbol, "expiry_epoch": expiry_epoch}

    monkeypatch.setattr(time, "monotonic", lambda: next(monotonic_values))
    with (
        patch(
            "trading.runtime.paper_session.build_option_candidates",
            return_value=(monthly_rows, {}),
        ),
        patch(
            "trading.runtime.paper_session.normalize_fyers_option_chain",
            return_value=_chain(),
        ),
    ):
        near = f.snapshot(
            contract=f.option_contract(
                symbol="NIFTY26SEP24000CE", strike=Decimal("24000")
            ),
            derivatives=DerivativesContext(
                days_to_expiry=1,
                open_interest=5000,
                option_type=OptionType.CALL,
                underlying_price=f.price("24000"),
            ),
        )
        catalog = InstrumentSpecStore(tmp_path / "instruments")
        underlying = f.snapshot(contract=f.index_contract())
        as_of = datetime(2026, 9, 28, 4, 0, tzinfo=UTC)
        feed = _Feed()
        common = dict(
            chain=_chain(),
            catalog=catalog,
            underlying=underlying,
            as_of=as_of,
            zone=IST,
            feed=feed,
            pipeline_symbol="NSE:NIFTY50-INDEX",
            monthly_dte_min=POLICY.contracts.monthly_dte_min,
            monthly_dte_max=POLICY.contracts.monthly_dte_max,
            cache_ttl_seconds=120,
        )
        _, _, _, cache = _merge_monthly_chain((near,), {}, **common)  # type: ignore[arg-type]
        _merge_monthly_chain((near,), {}, cache=cache, **common)  # type: ignore[arg-type]
        _merge_monthly_chain((near,), {}, cache=cache, **common)  # type: ignore[arg-type]
    assert fetch_calls == 2
