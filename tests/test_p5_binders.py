"""Phase P5 Mode-Specific Binders Verification Test Suite.

Locks in Phase P5 requirements per FOUR_MODE_LAYER_CHANGE_CRITERIA.md (§3.3)
and NIFTY_FOUR_MODE_CURSOR_REDESIGN.md (§3.3, §3.6):
- _abs_delta_in_range helper function.
- bind_m2_long_option:
  * Instrument master validation (reject unlisted, INSTRUMENT_MASTER_ABSENT).
  * Direction check (UP -> CALL, DOWN -> PUT, non-directional -> PRICE_UNAVAILABLE).
  * Following-week expiry selection (0/1 DTE ban -> EXPIRY_0_1_DTE_EXCLUDED).
  * Holiday substitution (audit and score components).
  * Monthly contract substitution (audit and score components).
  * Fallback expiry logic.
  * Delta band [0.45, 0.65].
  * Ranking, selection, SetupFeatures with score_components (m2_dte, is_holiday_substituted, is_monthly_substituted).
- bind_m1_cas_option:
  * Instrument master validation.
  * Direction check.
  * 0-DTE ban default off (allow_0dte=False vs allow_0dte=True).
  * Moderately OTM delta band [0.20, 0.40].
  * Strategy id "cas_microstructure", structure CAS_OPTION.
- Strike separation: M1 and M2 select different strikes on the same chain.
- Backwards compatibility of bind_long_option.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import tests.factories as f
from trading.domain.contracts import FeatureSnapshot, MarketState
from trading.domain.contracts.identification import (
    MacroStatus,
    StructureKind,
    TrendState,
    VolatilityState,
)
from trading.domain.contracts.snapshot import DerivativesContext, Greeks
from trading.domain.enums import DataQuality, OptionType, ReasonCode
from trading.identification import (
    bind_long_option,
    bind_m1_cas_option,
    bind_m2_long_option,
    load_identification_policy,
)
from trading.identification.binders import _abs_delta_in_range
from trading.identification.calendar import (
    DEFAULT_NSE_2026_HOLIDAYS,
    TradingCalendarPort,
    get_calendar_port,
)

ROOT = Path(__file__).parents[1]
POLICY = load_identification_policy(ROOT / "config" / "identification.yaml")
KOLKATA = ZoneInfo("Asia/Kolkata")
# 2026-09-24 10:00 IST is a Thursday
CALCULATED_AT = datetime(2026, 9, 24, 10, 0, tzinfo=KOLKATA)


def _market(
    trend: TrendState = TrendState.UP,
    calculated_at: datetime = CALCULATED_AT,
    **overrides: object,
) -> MarketState:
    payload: dict[str, object] = {
        "market_state_id": "market-p5",
        "feature_version": POLICY.feature_version,
        "calculated_at": calculated_at.astimezone(UTC),
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
        "trend_score": Decimal("0.7") if trend is TrendState.UP else Decimal("-0.7"),
        "event_state": "NORMAL",
        "macro_status": MacroStatus.NEUTRAL,
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
    expiry: date,
    dte: int,
    bid: str = "100",
    ask: str = "101",
    option_type: OptionType = OptionType.CALL,
    oi: int = 5000,
    volume: int = 5000,
) -> FeatureSnapshot:
    return f.snapshot(
        snapshot_id=f"snapshot-{symbol}",
        contract=f.option_contract(
            symbol=symbol,
            expiry=expiry,
            strike=Decimal(strike),
            option_type=option_type,
        ),
        market=f.quote(
            bid=f.price(bid),
            ask=f.price(ask),
            last=f.price(bid),
            volume=volume,
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


# =========================================================================
# 1. _abs_delta_in_range Helper Tests
# =========================================================================


def test_abs_delta_in_range_truth_table() -> None:
    """_abs_delta_in_range tests lower, upper, inside, outside, and None delta."""
    call_50 = _option(
        "C50", strike="24000", delta="0.50", expiry=date(2026, 10, 1), dte=7
    )
    put_50 = _option(
        "P50",
        strike="24000",
        delta="-0.50",
        expiry=date(2026, 10, 1),
        dte=7,
        option_type=OptionType.PUT,
    )
    call_30 = _option(
        "C30", strike="24500", delta="0.30", expiry=date(2026, 10, 1), dte=7
    )

    # In [0.45, 0.65]
    assert _abs_delta_in_range(call_50, Decimal("0.45"), Decimal("0.65")) is True
    assert _abs_delta_in_range(put_50, Decimal("0.45"), Decimal("0.65")) is True
    assert _abs_delta_in_range(call_30, Decimal("0.45"), Decimal("0.65")) is False

    # In [0.20, 0.40]
    assert _abs_delta_in_range(call_30, Decimal("0.20"), Decimal("0.40")) is True
    assert _abs_delta_in_range(call_50, Decimal("0.20"), Decimal("0.40")) is False

    # Boundary conditions
    assert _abs_delta_in_range(call_50, Decimal("0.50"), Decimal("0.60")) is True
    assert _abs_delta_in_range(call_50, Decimal("0.40"), Decimal("0.50")) is True

    # Missing greeks
    no_greeks = f.snapshot(snapshot_id="no-greeks")
    assert _abs_delta_in_range(no_greeks, Decimal("0.20"), Decimal("0.60")) is False


# =========================================================================
# 2. Mode 2 bind_m2_long_option Tests
# =========================================================================


def test_m2_instrument_master_validation() -> None:
    """bind_m2_long_option rejects candidates absent from master_symbols."""
    market = _market(TrendState.UP)
    # Target expiry Thursday 2026-10-01 (as_of 2026-09-24)
    c1 = _option(
        "NIFTY26OCT24000CE",
        strike="24000",
        delta="0.52",
        expiry=date(2026, 10, 1),
        dte=7,
    )
    c2 = _option(
        "NIFTY26OCT24100CE",
        strike="24100",
        delta="0.48",
        expiry=date(2026, 10, 1),
        dte=7,
    )

    # 1. No candidates exist in master_symbols -> INSTRUMENT_MASTER_ABSENT
    bound = bind_m2_long_option(
        (c1, c2),
        market=market,
        policy=POLICY,
        master_symbols=frozenset({"OTHER_SYMBOL"}),
    )
    assert bound.binding.eligible is False
    assert bound.binding.reason_codes == (ReasonCode.INSTRUMENT_MASTER_ABSENT,)
    assert bound.binding.selected_symbols == ()
    assert bound.binding.rejected_symbols == ("NIFTY26OCT24000CE", "NIFTY26OCT24100CE")
    assert bound.setup_features is None

    # 2. Partial presence: c1 in master, c2 not in master -> only c1 eligible
    bound_partial = bind_m2_long_option(
        (c1, c2),
        market=market,
        policy=POLICY,
        master_symbols=frozenset({"NIFTY26OCT24000CE"}),
    )
    assert bound_partial.binding.eligible is True
    assert bound_partial.binding.selected_symbols == ("NIFTY26OCT24000CE",)
    assert bound_partial.binding.rejected_symbols == ("NIFTY26OCT24100CE",)


def test_m2_direction_check() -> None:
    """bind_m2_long_option respects market trend and rejects non-directional states."""
    c_call = _option(
        "CALL_OPT",
        strike="24000",
        delta="0.52",
        expiry=date(2026, 10, 1),
        dte=7,
        option_type=OptionType.CALL,
    )
    c_put = _option(
        "PUT_OPT",
        strike="24000",
        delta="-0.52",
        expiry=date(2026, 10, 1),
        dte=7,
        option_type=OptionType.PUT,
    )

    # UP trend selects CALL
    bound_up = bind_m2_long_option(
        (c_call, c_put), market=_market(TrendState.UP), policy=POLICY
    )
    assert bound_up.binding.eligible is True
    assert bound_up.binding.selected_symbols == ("CALL_OPT",)

    # DOWN trend selects PUT
    bound_down = bind_m2_long_option(
        (c_call, c_put), market=_market(TrendState.DOWN), policy=POLICY
    )
    assert bound_down.binding.eligible is True
    assert bound_down.binding.selected_symbols == ("PUT_OPT",)

    # RANGE trend -> PRICE_UNAVAILABLE
    bound_range = bind_m2_long_option(
        (c_call, c_put), market=_market(TrendState.RANGE), policy=POLICY
    )
    assert bound_range.binding.eligible is False
    assert bound_range.binding.reason_codes == (ReasonCode.PRICE_UNAVAILABLE,)
    assert bound_range.setup_features is None


def test_m2_0_1_dte_excluded() -> None:
    """bind_m2_long_option rejects chains with only 0/1 DTE expiries with EXPIRY_0_1_DTE_EXCLUDED."""
    market = _market(TrendState.UP, calculated_at=CALCULATED_AT)
    # Today is 2026-09-24 (0 DTE) and tomorrow is 2026-09-25 (1 DTE)
    c0 = _option("DTE0", strike="24000", delta="0.52", expiry=date(2026, 9, 24), dte=0)
    c1 = _option("DTE1", strike="24000", delta="0.52", expiry=date(2026, 9, 25), dte=1)

    bound = bind_m2_long_option((c0, c1), market=market, policy=POLICY)
    assert bound.binding.eligible is False
    assert bound.binding.reason_codes == (ReasonCode.EXPIRY_0_1_DTE_EXCLUDED,)
    assert bound.candidates == ()
    assert bound.setup_features is None


def test_m2_following_week_expiry_selected() -> None:
    """bind_m2_long_option selects following week expiry, ignoring 0-DTE on same chain."""
    market = _market(TrendState.UP, calculated_at=CALCULATED_AT)
    c0 = _option(
        "TODAY_0DTE", strike="24000", delta="0.52", expiry=date(2026, 9, 24), dte=0
    )
    c_next = _option(
        "NEXT_WEEK",
        strike="24000",
        delta="0.52",
        expiry=date(2026, 10, 1),
        dte=7,
    )

    bound = bind_m2_long_option((c0, c_next), market=market, policy=POLICY)
    assert bound.binding.eligible is True
    assert bound.binding.selected_symbols == ("NEXT_WEEK",)
    assert bound.setup_features is not None
    assert bound.setup_features.structure == StructureKind.LONG_OPTION
    assert bound.setup_features.score_components["m2_dte"] == Decimal(7)
    assert bound.setup_features.score_components["is_holiday_substituted"] == Decimal(0)
    assert bound.setup_features.score_components["is_monthly_substituted"] == Decimal(0)


def test_m2_holiday_substitution() -> None:
    """bind_m2_long_option detects holiday substitution and reflects in score_components."""
    oct_08 = datetime(2026, 10, 8, 10, 0, tzinfo=KOLKATA)
    market = _market(TrendState.UP, calculated_at=oct_08)
    # Suppose Thursday 2026-10-15 is a holiday, exchange lists Wednesday 2026-10-14
    custom_holidays = DEFAULT_NSE_2026_HOLIDAYS | {date(2026, 10, 15)}
    cal = TradingCalendarPort(holidays=custom_holidays)

    c_wed = _option(
        "WED_EXPIRY",
        strike="24000",
        delta="0.52",
        expiry=date(2026, 10, 14),
        dte=6,
    )

    bound = bind_m2_long_option((c_wed,), market=market, policy=POLICY, calendar=cal)
    assert bound.binding.eligible is True
    assert bound.binding.selected_symbols == ("WED_EXPIRY",)
    assert bound.setup_features is not None
    assert bound.setup_features.score_components["is_holiday_substituted"] == Decimal(1)
    assert bound.setup_features.score_components["is_monthly_substituted"] == Decimal(0)
    assert bound.setup_features.score_components["m2_dte"] == Decimal(6)


def test_m2_monthly_substitution() -> None:
    """bind_m2_long_option detects monthly contract substitution when expiry is last Thursday."""
    # As of Thursday 2026-09-17: following week Thursday is 2026-09-24 (last Thursday of September)
    sep_17 = datetime(2026, 9, 17, 10, 0, tzinfo=KOLKATA)
    market = _market(TrendState.UP, calculated_at=sep_17)

    c_monthly = _option(
        "SEP_MONTHLY",
        strike="24000",
        delta="0.52",
        expiry=date(2026, 9, 24),
        dte=7,
    )

    bound = bind_m2_long_option((c_monthly,), market=market, policy=POLICY)
    assert bound.binding.eligible is True
    assert bound.binding.selected_symbols == ("SEP_MONTHLY",)
    assert bound.setup_features is not None
    assert bound.setup_features.score_components["is_monthly_substituted"] == Decimal(1)
    assert bound.setup_features.score_components["m2_dte"] == Decimal(7)


def test_m2_fallback_expiry() -> None:
    """bind_m2_long_option honors allow_fallback_expiry when target week has no contract."""
    market = _market(TrendState.UP, calculated_at=CALCULATED_AT)
    # Target week is Sep 28 - Oct 04. No contract listed in that week. Next listed is Oct 08.
    c_oct8 = _option(
        "OCT08",
        strike="24000",
        delta="0.52",
        expiry=date(2026, 10, 8),
        dte=14,
    )

    # Without allow_fallback_expiry -> CALENDAR_NO_ELIGIBLE_EXPIRY
    bound_no_fb = bind_m2_long_option(
        (c_oct8,), market=market, policy=POLICY, allow_fallback_expiry=False
    )
    assert bound_no_fb.binding.eligible is False
    assert bound_no_fb.binding.reason_codes == (ReasonCode.CALENDAR_NO_ELIGIBLE_EXPIRY,)

    # With allow_fallback_expiry -> selects Oct 08
    bound_fb = bind_m2_long_option(
        (c_oct8,), market=market, policy=POLICY, allow_fallback_expiry=True
    )
    assert bound_fb.binding.eligible is True
    assert bound_fb.binding.selected_symbols == ("OCT08",)
    assert bound_fb.setup_features is not None
    assert bound_fb.setup_features.score_components["m2_dte"] == Decimal(14)


def test_m2_delta_band_rejection() -> None:
    """bind_m2_long_option rejects candidates outside delta [0.45, 0.65]."""
    market = _market(TrendState.UP, calculated_at=CALCULATED_AT)
    c_otm = _option(
        "OTM_35", strike="24500", delta="0.35", expiry=date(2026, 10, 1), dte=7
    )
    c_deep_itm = _option(
        "ITM_75", strike="23500", delta="0.75", expiry=date(2026, 10, 1), dte=7
    )

    bound = bind_m2_long_option((c_otm, c_deep_itm), market=market, policy=POLICY)
    assert bound.binding.eligible is False
    assert bound.candidates == ()


# =========================================================================
# 3. Mode 1 bind_m1_cas_option Tests
# =========================================================================


def test_m1_instrument_master_validation() -> None:
    """bind_m1_cas_option validates candidates against master_symbols."""
    market = _market(TrendState.UP)
    c1 = _option("SYM1", strike="24300", delta="0.30", expiry=date(2026, 9, 24), dte=5)

    bound = bind_m1_cas_option(
        (c1,),
        market=market,
        policy=POLICY,
        master_symbols=frozenset({"OTHER_SYM"}),
    )
    assert bound.binding.eligible is False
    assert bound.binding.reason_codes == (ReasonCode.INSTRUMENT_MASTER_ABSENT,)
    assert bound.binding.strategy_id == "cas_microstructure"


def test_m1_direction_check() -> None:
    """bind_m1_cas_option returns PRICE_UNAVAILABLE on non-directional trend."""
    c1 = _option("SYM1", strike="24300", delta="0.30", expiry=date(2026, 9, 24), dte=5)

    bound = bind_m1_cas_option((c1,), market=_market(TrendState.RANGE), policy=POLICY)
    assert bound.binding.eligible is False
    assert bound.binding.reason_codes == (ReasonCode.PRICE_UNAVAILABLE,)
    assert bound.binding.strategy_id == "cas_microstructure"


def test_m1_0_dte_off_by_default() -> None:
    """bind_m1_cas_option excludes 0-DTE by default; allows when allow_0dte=True."""
    market = _market(TrendState.UP, calculated_at=CALCULATED_AT)
    c0 = _option(
        "CAS_0DTE", strike="24300", delta="0.30", expiry=date(2026, 9, 24), dte=0
    )

    # Default allow_0dte=False -> excluded
    bound_disallowed = bind_m1_cas_option(
        (c0,), market=market, policy=POLICY, allow_0dte=False
    )
    assert bound_disallowed.binding.eligible is False
    assert bound_disallowed.binding.reason_codes == (
        ReasonCode.EXPIRY_0_1_DTE_EXCLUDED,
    )

    # allow_0dte=True -> permitted and selected
    bound_allowed = bind_m1_cas_option(
        (c0,), market=market, policy=POLICY, allow_0dte=True
    )
    assert bound_allowed.binding.eligible is True
    assert bound_allowed.binding.selected_symbols == ("CAS_0DTE",)
    assert bound_allowed.binding.strategy_id == "cas_microstructure"
    assert bound_allowed.setup_features is not None
    assert bound_allowed.setup_features.structure == StructureKind.CAS_OPTION


def test_m1_delta_band_moderately_otm() -> None:
    """bind_m1_cas_option selects [0.20, 0.40] and rejects ATM [0.45, 0.65]."""
    market = _market(TrendState.UP, calculated_at=CALCULATED_AT)
    c_atm = _option(
        "ATM_CALL", strike="24000", delta="0.52", expiry=date(2026, 10, 1), dte=7
    )
    c_otm = _option(
        "OTM_CALL", strike="24300", delta="0.30", expiry=date(2026, 10, 1), dte=7
    )

    bound = bind_m1_cas_option((c_atm, c_otm), market=market, policy=POLICY)
    assert bound.binding.eligible is True
    assert bound.binding.selected_symbols == ("OTM_CALL",)
    assert bound.binding.rejected_symbols == ("ATM_CALL",)
    assert bound.binding.strategy_id == "cas_microstructure"
    assert bound.setup_features is not None
    assert bound.setup_features.structure == StructureKind.CAS_OPTION


# =========================================================================
# 4. Mode 1 vs Mode 2 Strike Separation on Same Chain
# =========================================================================


def test_m1_and_m2_select_different_strikes_on_same_chain() -> None:
    """CRITERIA §3.3: M1 and M2 select different strikes on the exact same chain."""
    market = _market(TrendState.UP, calculated_at=CALCULATED_AT)
    expiry = date(2026, 10, 1)  # Following week Thursday

    c_atm_52 = _option(
        "NIFTY_ATM_24000",
        strike="24000",
        delta="0.52",
        expiry=expiry,
        dte=7,
    )
    c_otm_30 = _option(
        "NIFTY_OTM_24300",
        strike="24300",
        delta="0.30",
        expiry=expiry,
        dte=7,
    )
    chain = (c_atm_52, c_otm_30)

    # M2 selects ATM strike (0.52 delta in [0.45, 0.65])
    m2_result = bind_m2_long_option(chain, market=market, policy=POLICY)
    assert m2_result.binding.eligible is True
    assert m2_result.binding.selected_symbols == ("NIFTY_ATM_24000",)
    assert m2_result.binding.strategy_id == "positional_long_option"

    # M1 selects OTM strike (0.30 delta in [0.20, 0.40])
    m1_result = bind_m1_cas_option(chain, market=market, policy=POLICY)
    assert m1_result.binding.eligible is True
    assert m1_result.binding.selected_symbols == ("NIFTY_OTM_24300",)
    assert m1_result.binding.strategy_id == "cas_microstructure"

    # Verifies distinct strike selection
    assert m2_result.binding.selected_symbols != m1_result.binding.selected_symbols


# =========================================================================
# 5. Backwards Compatibility for bind_long_option
# =========================================================================


def test_bind_long_option_backwards_compatibility() -> None:
    """bind_long_option functions with legacy kwargs and optional calendar/master_symbols."""
    market = _market(TrendState.UP)
    c1 = _option(
        "LEGACY_CALL",
        strike="24000",
        delta="0.52",
        expiry=date(2026, 9, 24),
        dte=7,
    )

    # Legacy invocation (no calendar, no master_symbols)
    bound_legacy = bind_long_option((c1,), market=market, policy=POLICY)
    assert bound_legacy.binding.eligible is True
    assert bound_legacy.binding.selected_symbols == ("LEGACY_CALL",)

    # Invocation with master_symbols provided
    bound_with_master = bind_long_option(
        (c1,),
        market=market,
        policy=POLICY,
        master_symbols=frozenset({"LEGACY_CALL"}),
        calendar=get_calendar_port(),
    )
    assert bound_with_master.binding.eligible is True
    assert bound_with_master.binding.selected_symbols == ("LEGACY_CALL",)

    # Master symbols exclusion
    bound_absent = bind_long_option(
        (c1,),
        market=market,
        policy=POLICY,
        master_symbols=frozenset({"NON_EXISTENT"}),
    )
    assert bound_absent.binding.eligible is False
    assert bound_absent.binding.reason_codes == (ReasonCode.INSTRUMENT_MASTER_ABSENT,)
