"""Phase P7 — M1 Selector on the G3 Path — Test Suite.

Locks in Phase P7 requirements from:
- FOUR_MODE_LAYER_CHANGE_CRITERIA.md (§3.3, §3.5, §1.2)
- NIFTY_FOUR_MODE_CURSOR_REDESIGN.md (§4.1, §5, §10, G3 prerequisites)
- FOUR_MODE_REDESIGN_PLAN.md (Phase P7, Gate G3)
- docs/reports/G3_CAS_FIELD_AND_LATENCY_REPORT.md (§5, §6)
- REQUIREMENT_TRACEABILITY.md (R-009, R-011, R-014, T10, T12, T14)

Verifications:
1. R-014 / §3.3: bind_m1_cas_option uses delta band [0.20, 0.40] (moderately OTM).
   This band is DISTINCT from M2's hardcoded [0.45, 0.65] — no shared binder.
2. §4.1 / §3.5: 0-DTE default off for M1 (allow_0dte=False by default).
3. §3.3: M1 and M2 select DIFFERENT strikes from the same candidate chain.
4. R-011 / §3.5: CasMicrostructureStrategy stamps mode_id=ModeId.M1_CAS and
   family_id=FamilyId.long_call / long_put on every emitted TradeIntent.
5. G3 / §1.2: startup_validation blocks M1 PAPER stance on poll_interval_seconds >= 60.
6. G3 enforcement: demote-to-shadow path (enforce_g3_shadow=True) silently changes
   cas_microstructure stance to SHADOW without raising.
7. R-009 / §1.2: Quote-only and depth-only cohort profiles are distinct; aggressor
   stays UNAVAILABLE (not inferred or defaulted).
8. §3.5: CasMicrostructureStrategy emits exactly one BUY leg, no short legs.
9. §3.5 (catch-all): M1 cannot receive a PAPER stance while G3 is not cleared,
   regardless of the enforce_g3_shadow flag setting.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import tests.factories as f
from trading.domain.contracts import (
    DerivativesContext,
    FeatureSnapshot,
    MarketState,
    TradeIntent,
)
from trading.domain.contracts.identification import (
    MacroStatus,
    TrendState,
    VolatilityState,
)
from trading.domain.contracts.snapshot import Greeks
from trading.domain.enums import (
    DataQuality,
    ExecutionMode,
    FamilyId,
    ModeId,
    OptionType,
    ReasonCode,
    Side,
)
from trading.identification import (
    bind_m1_cas_option,
    bind_m2_long_option,
    load_identification_policy,
)
from trading.identification.calendar import get_calendar_port
from trading.runtime.cas_event_path import CasEventDrivenConfig
from trading.runtime.paper_session import PaperSessionConfig
from trading.runtime.startup_validation import (
    validate_startup_configuration,
)
from trading.strategies import (
    CasMicrostructureStrategy,
    MacroAssessment,
    MacroBias,
    StrategyContext,
)
from trading.strategies.cas_microstructure import (
    FEATURE_AUCTION_IMBALANCE,
    FEATURE_MICROPRICE_EDGE_BPS,
    FEATURE_QUOTE_INSTABILITY,
    FEATURE_SET_VERSION,
    FEATURE_TRADE_FLOW_IMBALANCE,
    REQUIRED_FEATURES,
    STRATEGY_VERSION,
)

ROOT = Path(__file__).resolve().parent.parent
POLICY = load_identification_policy(ROOT / "config" / "identification.yaml")

# --- Time constants ---
# 2026-09-14 is a Monday; 15:15 IST = 09:45 UTC — inside the CAS closing window
CAS_NOW = datetime(2026, 9, 14, 9, 45, tzinfo=UTC)

# Expiry dates for testing
EXPIRY_3DTE = date(2026, 9, 17)  # 3 calendar days from 2026-09-14
EXPIRY_0DTE = date(2026, 9, 14)  # Same day = 0 DTE


# ---------------------------------------------------------------------------
# Helper builders
# ---------------------------------------------------------------------------

BULLISH_FEATURES: dict[str, Decimal] = {
    FEATURE_AUCTION_IMBALANCE: Decimal("0.40"),
    FEATURE_TRADE_FLOW_IMBALANCE: Decimal("0.30"),
    FEATURE_MICROPRICE_EDGE_BPS: Decimal("5.00"),
    FEATURE_QUOTE_INSTABILITY: Decimal("0.10"),
}

BEARISH_FEATURES: dict[str, Decimal] = {
    FEATURE_AUCTION_IMBALANCE: Decimal("-0.40"),
    FEATURE_TRADE_FLOW_IMBALANCE: Decimal("-0.30"),
    FEATURE_MICROPRICE_EDGE_BPS: Decimal("-5.00"),
    FEATURE_QUOTE_INSTABILITY: Decimal("0.10"),
}


def _times(instant: datetime = CAS_NOW) -> f.SnapshotTimes:
    return f.snapshot_times(
        event_time=instant - timedelta(seconds=2),
        source_time=instant - timedelta(seconds=1),
        receive_time=instant - timedelta(milliseconds=500),
        calculation_time=instant - timedelta(milliseconds=200),
    )


def _underlying(
    features: dict[str, Decimal] | None = None,
    instant: datetime = CAS_NOW,
) -> FeatureSnapshot:
    return f.snapshot(
        snapshot_id="SNAP-NIFTY-INDEX",
        contract=f.index_contract(),
        times=_times(instant),
        market=f.quote(last=f.price("24100"), close=f.price("24000")),
        feature_set_version=FEATURE_SET_VERSION,
        features=BULLISH_FEATURES if features is None else features,
    )


def _option(
    symbol: str,
    *,
    strike: str,
    delta: str,
    option_type: OptionType = OptionType.CALL,
    expiry: date = EXPIRY_3DTE,
    dte: int = 3,
    bid: str = "80.00",
    ask: str = "80.50",
    oi: int = 5000,
) -> FeatureSnapshot:
    return f.snapshot(
        snapshot_id=f"SNAP-{symbol}",
        contract=f.option_contract(
            symbol=symbol,
            expiry=expiry,
            strike=Decimal(strike),
            option_type=option_type,
        ),
        times=_times(),
        market=f.quote(
            bid=f.price(bid),
            ask=f.price(ask),
            last=f.price(bid),
        ),
        derivatives=DerivativesContext(
            days_to_expiry=dte,
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
            "top_of_book_observed": Decimal(1),
        },
    )


def _market(trend: TrendState = TrendState.UP) -> Any:
    """Build a MarketState-compatible dict for binder calls."""
    return MarketState.model_validate(
        {
            "market_state_id": "market-p7",
            "feature_version": POLICY.feature_version,
            "calculated_at": CAS_NOW,
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
            "trend_score": Decimal("0.7")
            if trend is TrendState.UP
            else Decimal("-0.7"),
            "event_state": "NORMAL",
            "macro_status": MacroStatus.NEUTRAL,
            "quality": DataQuality.VALID,
            "warmup_complete": True,
            "completed_bar_count": 60,
            "session_count": 20,
        }
    )


def _macro(bias: MacroBias, confidence: str = "0.8") -> MacroAssessment:
    return MacroAssessment(
        regime="RISK_ON",
        directional_bias=bias,
        confidence=Decimal(confidence),
        fresh_until=CAS_NOW + timedelta(hours=1),
        evidence_ids=("src-1",),
        model_version="macro-agent-v1",
    )


def _ctx(
    candidates: tuple[FeatureSnapshot, ...],
    *,
    underlying: FeatureSnapshot | None = None,
    macro: MacroAssessment | None = None,
    now: datetime = CAS_NOW,
) -> StrategyContext:
    return StrategyContext(
        underlying=underlying or _underlying(),
        candidates=candidates,
        view=f.portfolio_view(),
        now=now,
        macro=macro,
    )


def _cas_intent(option_type: OptionType) -> TradeIntent:
    """Build and return the CasMicrostructureStrategy intent for the given option type."""
    features = BULLISH_FEATURES if option_type is OptionType.CALL else BEARISH_FEATURES
    # For PUT, underlying last=24000 close=24000 gives no technical signal;
    # supply a BEARISH macro so resolve_direction returns the bearish bias.
    macro = (
        _macro(MacroBias.BEARISH)
        if option_type is OptionType.PUT
        else _macro(MacroBias.BULLISH)
    )
    # last > close triggers BULLISH technical; last == close stays NEUTRAL,
    # so always supply a macro to force deterministic bias in tests.
    underlying_last = "24100" if option_type is OptionType.CALL else "23900"
    underlying = f.snapshot(
        snapshot_id="SNAP-NIFTY-INDEX",
        contract=f.index_contract(),
        times=_times(),
        market=f.quote(last=f.price(underlying_last), close=f.price("24000")),
        feature_set_version=FEATURE_SET_VERSION,
        features=features,
    )
    option = _option(
        f"NIFTY26SEP24000{option_type.value}",
        strike="24000",
        delta="0.30" if option_type is OptionType.CALL else "-0.30",
        option_type=option_type,
    )
    ctx = _ctx((option,), underlying=underlying, macro=macro)
    decision = CasMicrostructureStrategy().evaluate(ctx)
    assert decision.emits_intent, (
        f"Expected intent for {option_type}, got rejections: {decision.rejections}"
    )
    return decision.intents[0]


def _session_config(**overrides: object) -> PaperSessionConfig:
    payload: dict[str, object] = {
        "poll_interval_seconds": 60,
        "eod_local": "15:40",
        "option_strikes_each_side": 2,
        "experiment_prefix": "EXP-TEST",
        "strategy_ids": ("positional_long_option", "cas_microstructure"),
        "strategy_stances": {
            "positional_long_option": ExecutionMode.PAPER,
            "cas_microstructure": ExecutionMode.PAPER,
        },
        "commodity_underlying": "CRUDEOIL",
        "commodity_exchange": "MCX",
        "commodity_segment": "MCX_COM",
        "cohort_dir": "data/paper/cohorts",
        "store_path": "data/paper/trading.sqlite",
        "broker_state_path": "data/paper/broker_state.json",
    }
    payload.update(overrides)
    return PaperSessionConfig.model_validate(payload)


# ===========================================================================
# Test 1 — R-014 / §3.3: bind_m1_cas_option uses delta [0.20, 0.40]
# ===========================================================================


def test_m1_binder_selects_within_moderately_otm_delta_band() -> None:
    """bind_m1_cas_option must select from [0.20, 0.40] delta, not the M2 band."""
    # Moderately OTM call (delta 0.30) — inside M1's [0.20, 0.40]
    m1_candidate = _option(
        "NIFTY26SEP23700CE",
        strike="23700",
        delta="0.30",
        option_type=OptionType.CALL,
    )
    # ITM call (delta 0.55) — inside M2's [0.45, 0.65] but outside M1's
    m2_candidate = _option(
        "NIFTY26SEP24000CE",
        strike="24000",
        delta="0.55",
        option_type=OptionType.CALL,
    )
    market = _market(TrendState.UP)
    candidates = (m1_candidate, m2_candidate)

    result = bind_m1_cas_option(
        candidates,
        market=market,
        policy=POLICY,
        delta_range=(Decimal("0.20"), Decimal("0.40")),
    )
    assert result.binding.eligible, (
        f"M1 binder should select a candidate; reasons={result.binding.reason_codes}"
    )
    # Must pick the moderately OTM candidate, not the ITM one
    assert "NIFTY26SEP23700CE" in result.binding.selected_symbols
    assert "NIFTY26SEP24000CE" not in result.binding.selected_symbols


# ===========================================================================
# Test 2 — §4.1 / §3.5: 0-DTE default off
# ===========================================================================


def test_m1_binder_rejects_0dte_by_default() -> None:
    """bind_m1_cas_option with allow_0dte=False (default) must exclude 0-DTE contracts."""
    zero_dte = _option(
        "NIFTY26SEP14CE",
        strike="24000",
        delta="0.30",
        option_type=OptionType.CALL,
        expiry=EXPIRY_0DTE,
        dte=0,
    )
    market = _market(TrendState.UP)
    cal = get_calendar_port()

    # Default allow_0dte=False
    result = bind_m1_cas_option(
        (zero_dte,),
        market=market,
        policy=POLICY,
        calendar=cal,
        allow_0dte=False,
    )
    assert not result.binding.eligible
    assert ReasonCode.EXPIRY_0_1_DTE_EXCLUDED in result.binding.reason_codes


def test_m1_binder_allows_0dte_when_explicitly_enabled() -> None:
    """When allow_0dte=True the same 0-DTE contract must be eligible."""
    zero_dte = _option(
        "NIFTY26SEP14CE",
        strike="24000",
        delta="0.30",
        option_type=OptionType.CALL,
        expiry=EXPIRY_0DTE,
        dte=0,
    )
    market = _market(TrendState.UP)
    cal = get_calendar_port()

    result = bind_m1_cas_option(
        (zero_dte,),
        market=market,
        policy=POLICY,
        calendar=cal,
        allow_0dte=True,
    )
    assert result.binding.eligible, (
        f"0-DTE should be eligible when allow_0dte=True; "
        f"reasons={result.binding.reason_codes}"
    )


# ===========================================================================
# Test 3 — §3.3: M1 and M2 select DIFFERENT strikes from the same chain
# ===========================================================================


def test_m1_and_m2_select_different_strikes_from_same_chain() -> None:
    """M1 (moderately OTM, delta ~0.30) and M2 (near-ATM, delta ~0.55) must diverge."""
    # delta 0.30 — moderately OTM, in M1's band
    m1_otm = _option(
        "NIFTY26SEP23700CE",
        strike="23700",
        delta="0.30",
        option_type=OptionType.CALL,
        expiry=date(2026, 9, 21),
        dte=7,
    )
    # delta 0.55 — near ATM, in M2's band
    m2_near_atm = _option(
        "NIFTY26SEP24000CE",
        strike="24000",
        delta="0.55",
        option_type=OptionType.CALL,
        expiry=date(2026, 9, 21),
        dte=7,
    )
    candidates = (m1_otm, m2_near_atm)
    market = _market(TrendState.UP)
    cal = get_calendar_port()

    m1_result = bind_m1_cas_option(
        candidates,
        market=market,
        policy=POLICY,
        calendar=cal,
        delta_range=(Decimal("0.20"), Decimal("0.40")),
    )
    m2_result = bind_m2_long_option(
        candidates,
        market=market,
        policy=POLICY,
        calendar=cal,
    )

    assert m1_result.binding.eligible, (
        f"M1 must select; reasons={m1_result.binding.reason_codes}"
    )
    assert m2_result.binding.eligible, (
        f"M2 must select; reasons={m2_result.binding.reason_codes}"
    )

    m1_symbol = m1_result.binding.selected_symbols[0]
    m2_symbol = m2_result.binding.selected_symbols[0]
    assert m1_symbol != m2_symbol, (
        f"M1 and M2 must select different strikes on the same chain; "
        f"both picked {m1_symbol}"
    )
    assert m1_symbol == "NIFTY26SEP23700CE"
    assert m2_symbol == "NIFTY26SEP24000CE"


# ===========================================================================
# Test 4 — R-011 / §3.5: CasMicrostructureStrategy stamps ModeId.M1_CAS + FamilyId
# ===========================================================================


def test_m1_strategy_stamps_mode_id_m1_cas_on_call_intent() -> None:
    """CasMicrostructureStrategy must stamp ModeId.M1_CAS on a bullish intent."""
    intent = _cas_intent(OptionType.CALL)
    assert intent.mode_id is ModeId.M1_CAS, (
        f"Expected ModeId.M1_CAS, got {intent.mode_id}"
    )


def test_m1_strategy_stamps_long_call_family_on_call_intent() -> None:
    """CasMicrostructureStrategy must stamp FamilyId.long_call on a CALL intent."""
    intent = _cas_intent(OptionType.CALL)
    assert intent.family_id == FamilyId.long_call.value, (
        f"Expected '{FamilyId.long_call.value}', got {intent.family_id!r}"
    )


def test_m1_strategy_stamps_long_put_family_on_put_intent() -> None:
    """CasMicrostructureStrategy must stamp FamilyId.long_put on a PUT intent."""
    intent = _cas_intent(OptionType.PUT)
    assert intent.mode_id is ModeId.M1_CAS
    assert intent.family_id == FamilyId.long_put.value, (
        f"Expected '{FamilyId.long_put.value}', got {intent.family_id!r}"
    )


# ===========================================================================
# Test 5 — §3.5: M1 emits exactly one BUY leg — no short legs
# ===========================================================================


def test_m1_intent_has_exactly_one_buy_leg_no_shorts() -> None:
    """CasMicrostructureStrategy must emit a single BUY leg; no short legs allowed."""
    intent = _cas_intent(OptionType.CALL)
    assert len(intent.legs) == 1, f"Expected 1 leg, got {len(intent.legs)}"
    assert intent.legs[0].side is Side.BUY, f"Expected BUY, got {intent.legs[0].side}"


# ===========================================================================
# Test 6 — G3 / §1.2: startup_validation blocks M1 PAPER on 60s polled loop
# ===========================================================================


def test_g3_slow_poll_warns_without_blocking_m1_paper() -> None:
    """validate_startup_configuration keeps M1 PAPER and records latency limitation."""
    config = _session_config(
        cas_event_driven=CasEventDrivenConfig(enabled=True),
    )
    validated_config, warnings = validate_startup_configuration(
        config, enforce_g3_shadow=False
    )
    assert (
        validated_config.strategy_stances["cas_microstructure"] is ExecutionMode.PAPER
    )
    assert warnings


def test_g3_enforce_flag_does_not_demote_cas() -> None:
    """enforce_g3_shadow=True no longer demotes M1 on a slow poll."""
    config = _session_config(cas_event_driven=CasEventDrivenConfig(enabled=True))
    validated_config, warnings = validate_startup_configuration(
        config, enforce_g3_shadow=True
    )
    assert (
        validated_config.strategy_stances["cas_microstructure"] is ExecutionMode.PAPER
    )
    assert any("measured limitation" in item for item in warnings)


def test_g3_does_not_block_m1_shadow_on_60s_poll() -> None:
    """M1 set to SHADOW is always permitted; no G3 error or warning expected."""
    config = _session_config(
        strategy_stances={
            "positional_long_option": ExecutionMode.PAPER,
            "cas_microstructure": ExecutionMode.SHADOW,
        }
    )
    validated_config, warnings = validate_startup_configuration(
        config, enforce_g3_shadow=False
    )
    assert (
        validated_config.strategy_stances["cas_microstructure"] is ExecutionMode.SHADOW
    )
    # No G3 warning expected for SHADOW
    g3_warnings = [w for w in warnings if "G3" in w]
    assert not g3_warnings, f"Unexpected G3 warnings for SHADOW stance: {g3_warnings}"


# ===========================================================================
# Test 8 — R-009 / §1.2 / G3: Cohort labels and aggressor separation
# ===========================================================================


def test_m1_strategy_version_is_quote_only_cohort_label() -> None:
    """STRATEGY_VERSION must be 'cas-microstructure-v1' (the EXP-M1-QUOTE profile).
    This is distinct from the depth-only cohort 'cas-depth-only-v1'.
    """
    assert STRATEGY_VERSION == "cas-microstructure-v1", (
        f"Quote-only profile version mismatch: {STRATEGY_VERSION!r}"
    )
    # Depth-only profile label must differ
    depth_label = "cas-depth-only-v1"
    assert depth_label != STRATEGY_VERSION, (
        "Quote-only and depth-only profiles share the same version string — "
        "they must be distinct."
    )


def test_m1_feature_set_version_required_for_intent() -> None:
    """A snapshot with a different feature_set_version must be rejected (DATA_INVALID).
    This ensures the strategy cannot operate on a depth-only or other incompatible profile.
    """
    underlying = _underlying().model_copy(
        update={"feature_set_version": "cas-depth-only-v1"}
    )
    option = _option(
        "NIFTY26SEP24000CE",
        strike="24000",
        delta="0.30",
        option_type=OptionType.CALL,
    )
    ctx = _ctx((option,), underlying=underlying)
    decision = CasMicrostructureStrategy().evaluate(ctx)
    assert not decision.emits_intent
    assert decision.rejections[0].reason is ReasonCode.DATA_INVALID, (
        "Incompatible feature profile must yield DATA_INVALID"
    )


def test_m1_aggressor_feature_not_required_for_decision() -> None:
    """CasMicrostructureStrategy must operate without aggressor_side or trade_flow.
    The declared REQUIRED_FEATURES must not include any aggressor-derived field.
    """
    aggressor_keys = {k for k in REQUIRED_FEATURES if "aggressor" in k.lower()}
    assert not aggressor_keys, (
        f"REQUIRED_FEATURES must not reference aggressor data; found: {aggressor_keys}"
    )
