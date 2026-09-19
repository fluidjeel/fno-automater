"""Deterministic Layer 3 identification and one-winner routing."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import tests.factories as f
from trading.data.events import CanonicalMarketEvent
from trading.data.storage.instrument_store import InstrumentSpecStore
from trading.domain.contracts import (
    CandidateBinding,
    FeatureSnapshot,
    InstrumentSpec,
    MarketState,
    SetupFeatures,
)
from trading.domain.contracts.identification import (
    MacroStatus,
    StructureKind,
    TrendState,
    VolatilityState,
)
from trading.domain.contracts.snapshot import DerivativesContext, Greeks
from trading.domain.enums import (
    DataQuality,
    Exchange,
    InstrumentKind,
    OptionType,
    ReasonCode,
)
from trading.identification import (
    BoundCandidates,
    IvBucket,
    SessionBucket,
    allowed_families_for,
    bind_debit_spread,
    bind_long_option,
    build_market_state,
    iv_bucket_for,
    load_identification_policy,
    route_nifty_options,
    session_bucket_for,
)
from trading.identification.vix import resolve_vix_symbol

ROOT = Path(__file__).parents[1]
POLICY = load_identification_policy(ROOT / "config" / "identification.yaml")
NOW = datetime(2026, 9, 19, 10, 0, tzinfo=UTC)


def _market(**overrides: object) -> MarketState:
    payload: dict[str, object] = {
        "market_state_id": "market-1",
        "feature_version": POLICY.feature_version,
        "calculated_at": NOW,
        "source_snapshot_ids": ("snapshot-1",),
        "trend": TrendState.UP,
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
        "macro_status": MacroStatus.MISSING,
        "quality": DataQuality.VALID,
        "warmup_complete": True,
        "completed_bar_count": 60,
        "session_count": 20,
    }
    payload.update(overrides)
    return MarketState.model_validate(payload)


def _option(
    symbol: str,
    *,
    strike: str,
    delta: str,
    bid: str,
    ask: str,
    option_type: OptionType = OptionType.CALL,
    oi: int = 5000,
    observed: Decimal = Decimal(1),
) -> FeatureSnapshot:
    return f.snapshot(
        snapshot_id=f"snapshot-{symbol}",
        contract=f.option_contract(
            symbol=symbol,
            expiry=date(2026, 9, 24),
            strike=Decimal(strike),
            option_type=option_type,
        ),
        market=f.quote(
            bid=f.price(bid),
            ask=f.price(ask),
            last=f.price(bid),
            volume=5000,
        ),
        derivatives=DerivativesContext(
            days_to_expiry=5,
            open_interest=oi,
            option_type=option_type,
            greeks=Greeks(
                model="fixture",
                calculation_version="1",
                converged=True,
                implied_volatility=Decimal("15"),
                delta=Decimal(delta),
            ),
            underlying_price=f.price("24000"),
        ),
        features={
            "lot_size": Decimal(75),
            "top_of_book_observed": observed,
        },
    )


def _bars() -> CanonicalMarketEvent:
    rows: list[dict[str, int | str]] = []
    price = Decimal("23000")
    for session in range(20):
        session_start = NOW - timedelta(days=20 - session, hours=3)
        for offset in range(3):
            price += Decimal(10)
            rows.append(
                {
                    "timestamp": int(
                        (session_start + timedelta(minutes=offset * 5)).timestamp()
                    ),
                    "open": str(price - 2),
                    "high": str(price + 3),
                    "low": str(price - 3),
                    "close": str(price),
                    "volume": 1000 + offset,
                }
            )
    # This unfinished bar must never affect the state.
    rows.append(
        {
            "timestamp": int((NOW - timedelta(minutes=2)).timestamp()),
            "open": "100",
            "high": "100",
            "low": "90",
            "close": "90",
            "volume": 100000,
        }
    )
    return CanonicalMarketEvent(
        event_id="bars-1",
        provider="fixture",
        symbol="NIFTY",
        event_type="BAR_SNAPSHOT",
        event_time=NOW,
        source_time=NOW,
        receive_time=NOW,
        provider_sequence=None,
        payload={"resolution": "5", "bars": rows},
        raw_ref="fixture://bars",
        normalization_version="1",
    )


def _bound(strategy_id: str, score: str) -> BoundCandidates:
    structure = (
        StructureKind.LONG_OPTION
        if strategy_id == "positional_long_option"
        else StructureKind.DEBIT_SPREAD
    )
    setup = SetupFeatures(
        identification_rule_version=POLICY.policy_version,
        router_version=POLICY.router_version,
        market_state_id="market-1",
        raw_setup_score=Decimal(score),
        score_components={"contract_binding": Decimal(score)},
        trend=TrendState.UP,
        volatility=VolatilityState.NORMAL,
        structure=structure,
        dte=5,
        delta=Decimal("0.5"),
        implied_volatility=Decimal("15"),
        iv_percentile=Decimal("30"),
        iv_rv_ratio=Decimal("1"),
        open_interest=5000,
        spread_fraction=Decimal("0.01"),
        liquidity_rank=Decimal(score),
        event_state="NORMAL",
        macro_status=MacroStatus.MISSING,
    )
    return BoundCandidates(
        binding=CandidateBinding(
            strategy_id=strategy_id,
            binding_version=POLICY.binding_version,
            selected_symbols=(strategy_id,),
            score=Decimal(score),
            eligible=True,
        ),
        candidates=(),
        setup_features=setup,
    )


def test_market_state_uses_completed_bars_and_is_reproducible() -> None:
    candidates = (_option("LONG", strike="24000", delta="0.52", bid="99", ask="100"),)
    kwargs = {
        "underlying": f.snapshot(
            market=f.quote(last=f.price("23600"), close=f.price("23000"))
        ),
        "option_candidates": candidates,
        "iv_history": tuple(Decimal(index) for index in range(10, 30)),
        "event_risk": f.event_risk_state(
            as_of=NOW - timedelta(minutes=1),
            expires_at=NOW + timedelta(hours=1),
        ),
        "macro": None,
        "as_of": NOW,
        "policy": POLICY,
    }
    first = build_market_state(_bars(), **kwargs)  # type: ignore[arg-type]
    second = build_market_state(_bars(), **kwargs)  # type: ignore[arg-type]

    assert first == second
    assert first.completed_bar_count == 60
    assert first.session_count == 20
    assert first.trend is TrendState.UP
    assert first.warmup_complete


def test_candidate_bag_binds_one_long_and_one_ordered_debit_spread() -> None:
    candidates = (
        _option("NIFTY-23900-CE", strike="23900", delta="0.62", bid="140", ask="141"),
        _option("NIFTY-24000-CE", strike="24000", delta="0.525", bid="99", ask="100"),
        _option("NIFTY-24100-CE", strike="24100", delta="0.275", bid="72", ask="73"),
        _option("NIFTY-24200-CE", strike="24200", delta="0.15", bid="45", ask="46"),
    )

    long_option = bind_long_option(candidates, market=_market(), policy=POLICY)
    spread = bind_debit_spread(candidates, market=_market(), policy=POLICY)

    assert long_option.binding.selected_symbols == ("NIFTY-24000-CE",)
    assert spread.binding.selected_symbols == (
        "NIFTY-24000-CE",
        "NIFTY-24100-CE",
    )
    assert len(long_option.candidates) == 1
    assert len(spread.candidates) == 2


def test_synthetic_top_of_book_is_ineligible() -> None:
    candidate = _option(
        "NIFTY-24000-CE",
        strike="24000",
        delta="0.525",
        bid="99",
        ask="100",
        observed=Decimal(0),
    )
    result = bind_long_option((candidate,), market=_market(), policy=POLICY)
    assert not result.binding.eligible
    assert result.binding.reason_codes == (ReasonCode.PRICE_UNAVAILABLE,)


def test_router_selects_low_iv_long_option_and_keeps_spread_shadow() -> None:
    route, opportunities = route_nifty_options(
        _market(),
        long_option=_bound("positional_long_option", "0.90"),
        debit_spread=_bound("debit_spread", "0.75"),
        policy=POLICY,
    )
    assert route.paper_winner == "positional_long_option"
    assert route.shadow_alternatives == ("debit_spread",)
    assert sum(item.execution for item in opportunities) == 1


def test_router_abstains_on_tie_macro_conflict_and_cooldown() -> None:
    long_option = _bound("positional_long_option", "0.90")
    spread = _bound("debit_spread", "0.90")

    tie, _ = route_nifty_options(
        _market(),
        long_option=long_option,
        debit_spread=spread,
        policy=POLICY,
    )
    conflict, _ = route_nifty_options(
        _market(macro_status=MacroStatus.CONFLICT),
        long_option=long_option,
        debit_spread=spread,
        policy=POLICY,
    )
    cooldown, _ = route_nifty_options(
        _market(),
        long_option=long_option,
        debit_spread=spread,
        policy=POLICY,
        cooldown_active=True,
    )

    assert tie.paper_winner is None
    assert conflict.reason_codes == (ReasonCode.EVENT_BLACKOUT,)
    assert cooldown.reason_codes == (ReasonCode.SETUP_COOLDOWN,)


def test_router_selects_debit_spread_when_iv_is_expensive() -> None:
    route, _ = route_nifty_options(
        _market(iv_percentile=Decimal("70"), iv_rv_ratio=Decimal("1.3")),
        long_option=_bound("positional_long_option", "0.75"),
        debit_spread=_bound("debit_spread", "0.90"),
        policy=POLICY,
    )
    assert route.paper_winner == "debit_spread"


def test_eligible_binds_always_attach_setup_features() -> None:
    candidates = (
        _option("NIFTY-23900-CE", strike="23900", delta="0.62", bid="140", ask="141"),
        _option("NIFTY-24000-CE", strike="24000", delta="0.525", bid="99", ask="100"),
        _option("NIFTY-24100-CE", strike="24100", delta="0.275", bid="72", ask="73"),
        _option("NIFTY-24200-CE", strike="24200", delta="0.15", bid="45", ask="46"),
    )
    market = _market()
    long_option = bind_long_option(candidates, market=market, policy=POLICY)
    spread = bind_debit_spread(candidates, market=market, policy=POLICY)

    assert long_option.binding.eligible
    assert long_option.setup_features is not None
    assert spread.binding.eligible
    assert spread.setup_features is not None


def test_router_pass_records_failed_gate_ids_without_fake_features() -> None:
    long_option = _bound("positional_long_option", "0.90")
    spread = _bound("debit_spread", "0.90")

    route, opportunities = route_nifty_options(
        _market(),
        long_option=long_option,
        debit_spread=spread,
        policy=POLICY,
    )

    assert route.paper_winner is None
    assert route.failed_gate_ids == ("min_score_gap",)
    assert all(item.setup_features is not None for item in opportunities)


def test_vix_history_populates_iv_percentile_and_rv_ratio() -> None:
    """Synthetic India VIX series fills iv_percentile and iv_rv_ratio."""
    vix = tuple(Decimal(level) for level in range(10, 30))
    state = build_market_state(
        _bars(),
        underlying=f.snapshot(
            market=f.quote(last=f.price("23600"), close=f.price("23000"))
        ),
        option_candidates=(),
        event_risk=None,
        macro=None,
        as_of=NOW,
        policy=POLICY,
        vix_history=vix,
    )
    assert state.iv_percentile is not None
    assert state.iv_rv_ratio is not None
    assert ReasonCode.DATA_GAP not in state.reason_codes


def test_missing_vix_is_fail_visible() -> None:
    """No VIX history leaves IV fields null and raises DATA_GAP."""
    state = build_market_state(
        _bars(),
        underlying=f.snapshot(
            market=f.quote(last=f.price("23600"), close=f.price("23000"))
        ),
        option_candidates=(),
        event_risk=None,
        macro=None,
        as_of=NOW,
        policy=POLICY,
        vix_history=(),
    )
    assert state.iv_percentile is None
    assert state.iv_rv_ratio is None
    assert ReasonCode.DATA_GAP in state.reason_codes


def test_resolve_vix_symbol_matches_instrument_master(tmp_path: Path) -> None:
    """Configured VIX ticker must exist in the instrument master; never invent one."""
    store = InstrumentSpecStore(tmp_path)
    store.write(
        "NSE_CM",
        (
            InstrumentSpec(
                trading_symbol=POLICY.vix_symbol,
                exchange=Exchange.NSE,
                segment="NSE_CM",
                underlying="INDIAVIX",
                instrument_kind=InstrumentKind.INDEX,
                provider_token="1",
                exchange_token=1,
                lot_size=0,
                tick_size=Decimal("0.01"),
                price_precision=2,
                trading_session="0915-1530",
                source="test",
                verified_at=date(2026, 9, 11),
            ),
        ),
    )
    assert resolve_vix_symbol(POLICY, instrument_root=tmp_path) == "NSE:INDIAVIX-INDEX"


def test_allow_table_matrix_for_regime_buckets() -> None:
    """Regime cells: directional, multileg, CAS auction; commodity excluded."""
    # NOW is 10:00 UTC = 15:30 IST → AUCTION window in policy.
    assert session_bucket_for(NOW, POLICY) is SessionBucket.AUCTION

    continuous = NOW.replace(hour=5, minute=0)  # 10:30 IST
    assert session_bucket_for(continuous, POLICY) is SessionBucket.CONTINUOUS

    cases = [
        # trend, iv, event, calculated_at, expect_superset, expect_absent
        (
            "UP",
            "30",
            "NORMAL",
            NOW,
            {"positional_long_option", "debit_spread", "cas_microstructure"},
            {"defined_risk_multileg", "commodity_futures_trend"},
        ),
        (
            "DOWN",
            "55",
            "CAUTION",
            continuous,
            {"positional_long_option", "debit_spread"},
            {"cas_microstructure", "defined_risk_multileg", "commodity_futures_trend"},
        ),
        (
            "RANGE",
            "80",
            "NORMAL",
            continuous,
            {"defined_risk_multileg", "debit_spread"},
            {"cas_microstructure", "commodity_futures_trend"},
        ),
        (
            "RANGE",
            "50",
            "NORMAL",
            continuous,
            set(),
            {"defined_risk_multileg", "cas_microstructure", "commodity_futures_trend"},
        ),
        (
            "UP",
            "30",
            "NORMAL",
            continuous,
            {"positional_long_option", "debit_spread"},
            {"cas_microstructure", "commodity_futures_trend"},
        ),
        (
            "MIXED",
            "80",
            "NORMAL",
            NOW,
            {"cas_microstructure", "positional_long_option", "debit_spread"},
            {"defined_risk_multileg", "commodity_futures_trend"},
        ),
        (
            "UP",
            "30",
            "BLOCK_NEW",
            continuous,
            set(),
            {"positional_long_option", "debit_spread", "cas_microstructure"},
        ),
        (
            "RANGE",
            "80",
            "NORMAL",
            NOW,
            {"defined_risk_multileg", "debit_spread", "cas_microstructure"},
            {"commodity_futures_trend"},
        ),
    ]
    for trend, iv, event, when, expect_has, expect_missing in cases:
        market = _market(
            trend=TrendState(trend),
            iv_percentile=Decimal(iv),
            event_state=event,
            calculated_at=when,
        )
        allowed = allowed_families_for(market, POLICY)
        assert expect_has <= allowed, (trend, iv, event, when, allowed)
        assert allowed.isdisjoint(expect_missing), (trend, iv, event, when, allowed)


def test_router_intersects_allow_table_blocking_preferred_family() -> None:
    """Allow-table can block the preferred family even when binders are eligible."""
    market = _market(event_state="BLOCK_NEW")
    route, _ = route_nifty_options(
        market,
        long_option=_bound("positional_long_option", "0.90"),
        debit_spread=_bound("debit_spread", "0.75"),
        policy=POLICY,
    )
    assert route.paper_winner is None
    assert allowed_families_for(market, POLICY) == frozenset()


def test_iv_bucket_boundaries_follow_router_and_allow_table() -> None:
    assert iv_bucket_for(Decimal("40"), POLICY) is IvBucket.LOW
    assert iv_bucket_for(Decimal("41"), POLICY) is IvBucket.MID
    assert iv_bucket_for(Decimal("70"), POLICY) is IvBucket.HIGH
    assert iv_bucket_for(None, POLICY) is IvBucket.UNKNOWN
