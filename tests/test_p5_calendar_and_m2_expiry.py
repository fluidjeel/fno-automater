"""Phase P5 Integration & Scenario Verification Test Suite.

Locks in Phase P5 requirements from:
- FOUR_MODE_LAYER_CHANGE_CRITERIA.md (§1.10, §3.3)
- NIFTY_FOUR_MODE_CURSOR_REDESIGN.md (§3.3, §3.6, Scenarios T16 and T17)
- REQUIREMENT_TRACEABILITY.md (R-008, R-010, T16, T17)

Verifications:
1. R-008: Calendar port exists, clocks unchanged (15:30/15:40), verification note recorded.
2. §1.10: Special session outside 10:30 or 14:30 follows written skip rule.
3. R-010 & T16: M2 current expiry at 0/1 DTE: selects following eligible week or abstains with EXPIRY_0_1_DTE_EXCLUDED.
4. T17: M2 holiday/monthly substitution from instrument master; actual listed contract selected, no guessed symbol.
5. §3.3: M2 delta band [0.45, 0.65] filtering.
6. §3.3: M1 and M2 can select different strikes on the same chain (M1 moderately OTM [0.20, 0.40], M2 near-ATM [0.45, 0.65]).
7. §3.3: No binder emits a symbol absent from instrument master (INSTRUMENT_MASTER_ABSENT).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import tests.factories as f
from trading.domain.contracts import FeatureSnapshot, MarketState
from trading.domain.contracts.identification import (
    MacroStatus,
    TrendState,
    VolatilityState,
)
from trading.domain.contracts.snapshot import DerivativesContext, Greeks
from trading.domain.enums import DataQuality, OptionType, ReasonCode, ReviewSlotId
from trading.identification import (
    bind_m1_cas_option,
    bind_m2_long_option,
    load_identification_policy,
)
from trading.identification.calendar import (
    DEFAULT_NSE_2026_HOLIDAYS,
    SessionPhase,
    SpecialSessionRule,
    TradingCalendarPort,
    get_calendar_port,
)
from trading.runtime.review_schedule import ReviewSlot, due_review_slots

ROOT = Path(__file__).parents[1]
POLICY = load_identification_policy(ROOT / "config" / "identification.yaml")
KOLKATA = ZoneInfo("Asia/Kolkata")
CALCULATED_AT = datetime(2026, 9, 24, 10, 0, tzinfo=KOLKATA)


def _market(
    trend: TrendState = TrendState.UP,
    calculated_at: datetime = CALCULATED_AT,
    **overrides: object,
) -> MarketState:
    payload: dict[str, object] = {
        "market_state_id": "market-p5-int",
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
    option_type: OptionType = OptionType.CALL,
    open_interest: int = 5000,
    bid: str = "100.00",
    ask: str = "101.00",
) -> FeatureSnapshot:
    contract = f.option_contract(
        symbol=symbol,
        strike=strike,
        option_type=option_type,
        expiry=expiry,
    )
    bid_p = f.price(bid)
    ask_p = f.price(ask)
    mid_p = f.price(str((Decimal(bid) + Decimal(ask)) / 2))
    greeks = Greeks(
        model="fixture",
        calculation_version="1",
        delta=Decimal(delta),
        gamma=Decimal("0.001"),
        theta=Decimal("-10.0"),
        vega=Decimal("15.0"),
        implied_volatility=Decimal("0.18"),
        converged=True,
    )
    derivatives = DerivativesContext(
        days_to_expiry=dte,
        open_interest=open_interest,
        option_type=option_type,
        greeks=greeks,
        underlying_price=f.price("24000"),
    )
    return f.snapshot(
        snapshot_id=f"SNAP-{symbol}",
        contract=contract,
        market=f.quote(bid=bid_p, ask=ask_p, last=mid_p, close=mid_p),
        derivatives=derivatives,
        features={"lot_size": Decimal(75), "top_of_book_observed": Decimal(1)},
    )


def test_r008_calendar_port_and_unchanged_clocks() -> None:
    """Requirement R-008: Calendar port exists; clocks unchanged (15:30/15:40); verification note recorded."""
    port = get_calendar_port()
    assert port.calendar_version == "nse-fo-calendar-v1"
    assert port.effective_date == "2026-09-13"
    assert (
        "https://www.nseindia.com/resources/exchange-communication-holidays-price-bands"
        in port.source
    )

    # Clocks must remain exactly 15:30 and 15:40 until verified with broker
    assert port.continuous_close == time(15, 30)
    assert port.auction_close == time(15, 40)
    assert port.verify_clocks_unchanged() is True
    assert "clocks unverified" in port.verification_note
    assert "15:30 / 15:40" in port.verification_note


def test_section_1_10_special_session_written_skip_rule() -> None:
    """CRITERIA §1.10: A special session outside 10:30 or 14:30 follows a written skip rule."""
    special_date = date(2026, 11, 8)  # Diwali Muhurat evening session
    rule = SpecialSessionRule(
        session_date=special_date,
        open_time=time(18, 15),
        close_time=time(19, 15),
        description="Diwali Muhurat Trading Session",
        skip_standard_reviews=True,
    )
    cal = TradingCalendarPort(special_sessions={special_date: rule})

    # At 10:30 IST on special session date:
    moment_1030 = datetime(2026, 11, 8, 10, 30, tzinfo=KOLKATA)
    assert cal.should_skip_positional_review(moment_1030, time(10, 30)) is True

    # At 14:30 IST on special session date:
    moment_1430 = datetime(2026, 11, 8, 14, 30, tzinfo=KOLKATA)
    assert cal.should_skip_positional_review(moment_1430, time(14, 30)) is True

    # Inside the special session window (18:30 IST):
    moment_1830 = datetime(2026, 11, 8, 18, 30, tzinfo=KOLKATA)
    assert cal.session_phase(moment_1830) is SessionPhase.SPECIAL

    # due_review_slots integration: 10:30 and 14:30 slots are skipped
    slots = (
        ReviewSlot(slot_id=ReviewSlotId.NSE_MORNING, local=time(10, 30)),
        ReviewSlot(slot_id=ReviewSlotId.NSE_AFTERNOON, local=time(14, 30)),
    )
    due = due_review_slots(
        now_local=datetime(2026, 11, 8, 18, 45, tzinfo=KOLKATA),
        session_open=time(9, 15),
        eod=time(19, 15),
        slots=slots,
        recorded=frozenset(),
        calendar=cal,
    )
    assert due == ()


def test_scenario_t16_m2_0_and_1_dte_exclusion_and_abstention() -> None:
    """Scenario T16 (R-010): M2 current expiry at 0/1 DTE selects following eligible week or abstains."""
    # Current session is Thursday 2026-09-24 (0-DTE for Sep 24)
    market = _market(TrendState.UP, calculated_at=CALCULATED_AT)

    c_today_0dte = _option(
        "NIFTY_24SEP_0DTE",
        strike="24000",
        delta="0.52",
        expiry=date(2026, 9, 24),
        dte=0,
    )
    c_tomorrow_1dte = _option(
        "NIFTY_25SEP_1DTE",
        strike="24000",
        delta="0.52",
        expiry=date(2026, 9, 25),
        dte=1,
    )

    # Case A: Universe has only 0-DTE and 1-DTE contracts -> Abstains with EXPIRY_0_1_DTE_EXCLUDED
    bound_abstain = bind_m2_long_option(
        (c_today_0dte, c_tomorrow_1dte), market=market, policy=POLICY
    )
    assert bound_abstain.binding.eligible is False
    assert bound_abstain.binding.reason_codes == (ReasonCode.EXPIRY_0_1_DTE_EXCLUDED,)
    assert bound_abstain.binding.selected_symbols == ()
    assert bound_abstain.setup_features is None

    # Case B: Universe has 0-DTE contract AND following-week eligible contract (Thursday 2026-10-01, 7 DTE)
    c_next_week = _option(
        "NIFTY_01OCT_7DTE",
        strike="24000",
        delta="0.52",
        expiry=date(2026, 10, 1),
        dte=7,
    )
    bound_select = bind_m2_long_option(
        (c_today_0dte, c_tomorrow_1dte, c_next_week), market=market, policy=POLICY
    )
    assert bound_select.binding.eligible is True
    assert bound_select.binding.selected_symbols == ("NIFTY_01OCT_7DTE",)
    assert "NIFTY_24SEP_0DTE" in bound_select.binding.rejected_symbols
    assert "NIFTY_25SEP_1DTE" in bound_select.binding.rejected_symbols
    assert bound_select.setup_features is not None
    assert bound_select.setup_features.dte == 7
    assert bound_select.setup_features.score_components["m2_dte"] == Decimal(7)


def test_scenario_t17_m2_holiday_and_monthly_substitution_from_master() -> None:
    """Scenario T17: M2 holiday/monthly substitution selects actual listed contract, never a guessed symbol."""
    # Sub-case 1: Holiday Substitution
    # Current date is Thursday 2026-10-08. Following week Thursday 2026-10-15 is an exchange holiday.
    # The instrument master lists the Wednesday contract 2026-10-14.
    oct_08 = datetime(2026, 10, 8, 10, 0, tzinfo=KOLKATA)
    market_oct08 = _market(TrendState.UP, calculated_at=oct_08)
    custom_holidays = DEFAULT_NSE_2026_HOLIDAYS | {date(2026, 10, 15)}
    cal = TradingCalendarPort(holidays=custom_holidays)

    # Contract listed in master expiring on Wednesday 2026-10-14
    c_wed_listed = _option(
        "NIFTY_14OCT_WED",
        strike="24000",
        delta="0.52",
        expiry=date(2026, 10, 14),
        dte=6,
    )
    master_catalog = frozenset({"NIFTY_14OCT_WED"})

    bound_holiday = bind_m2_long_option(
        (c_wed_listed,),
        market=market_oct08,
        policy=POLICY,
        calendar=cal,
        master_symbols=master_catalog,
    )
    assert bound_holiday.binding.eligible is True
    assert bound_holiday.binding.selected_symbols == ("NIFTY_14OCT_WED",)
    assert bound_holiday.setup_features is not None
    assert bound_holiday.setup_features.score_components[
        "is_holiday_substituted"
    ] == Decimal(1)
    assert bound_holiday.setup_features.score_components["m2_dte"] == Decimal(6)

    # Sub-case 2: Monthly contract substitution
    # As of Thursday 2026-09-17: following week Thursday is 2026-09-24 (month-end contract)
    sep_17 = datetime(2026, 9, 17, 10, 0, tzinfo=KOLKATA)
    market_sep17 = _market(TrendState.UP, calculated_at=sep_17)
    c_monthly_listed = _option(
        "NIFTY_24SEP_MONTHLY",
        strike="24000",
        delta="0.52",
        expiry=date(2026, 9, 24),
        dte=7,
    )
    master_monthly = frozenset({"NIFTY_24SEP_MONTHLY"})

    bound_monthly = bind_m2_long_option(
        (c_monthly_listed,),
        market=market_sep17,
        policy=POLICY,
        master_symbols=master_monthly,
    )
    assert bound_monthly.binding.eligible is True
    assert bound_monthly.binding.selected_symbols == ("NIFTY_24SEP_MONTHLY",)
    assert bound_monthly.setup_features is not None
    assert bound_monthly.setup_features.score_components[
        "is_monthly_substituted"
    ] == Decimal(1)
    assert bound_monthly.setup_features.score_components["m2_dte"] == Decimal(7)


def test_section_3_3_m2_delta_band_045_to_065() -> None:
    """CRITERIA §3.3: M2 binder enforces delta band about [0.45, 0.65]."""
    market = _market(TrendState.UP, calculated_at=CALCULATED_AT)
    expiry = date(2026, 10, 1)

    c_far_otm = _option("OTM_30", strike="24400", delta="0.30", expiry=expiry, dte=7)
    c_marginal_otm = _option(
        "OTM_42", strike="24200", delta="0.42", expiry=expiry, dte=7
    )
    c_eligible_low = _option(
        "IN_BAND_48", strike="24100", delta="0.48", expiry=expiry, dte=7
    )
    c_eligible_atm = _option(
        "IN_BAND_54", strike="24000", delta="0.54", expiry=expiry, dte=7
    )
    c_eligible_high = _option(
        "IN_BAND_63", strike="23900", delta="0.63", expiry=expiry, dte=7
    )
    c_deep_itm = _option("ITM_72", strike="23700", delta="0.72", expiry=expiry, dte=7)

    chain = (
        c_far_otm,
        c_marginal_otm,
        c_eligible_low,
        c_eligible_atm,
        c_eligible_high,
        c_deep_itm,
    )
    bound = bind_m2_long_option(chain, market=market, policy=POLICY)

    assert bound.binding.eligible is True
    # The selected candidate must be one of the in-band contracts
    assert bound.binding.selected_symbols[0] in {
        "IN_BAND_48",
        "IN_BAND_54",
        "IN_BAND_63",
    }
    # Out of band contracts are rejected
    assert "OTM_30" in bound.binding.rejected_symbols
    assert "OTM_42" in bound.binding.rejected_symbols
    assert "ITM_72" in bound.binding.rejected_symbols


def test_section_3_3_m1_and_m2_select_different_strikes_on_same_chain() -> None:
    """CRITERIA §3.3: M1 and M2 select different strikes on the same chain."""
    market = _market(TrendState.UP, calculated_at=CALCULATED_AT)
    expiry = date(2026, 10, 1)

    c_atm = _option(
        "NIFTY_24000_CE", strike="24000", delta="0.52", expiry=expiry, dte=7
    )
    c_otm = _option(
        "NIFTY_24300_CE", strike="24300", delta="0.30", expiry=expiry, dte=7
    )
    chain = (c_atm, c_otm)

    # M2 selects near-ATM (delta 0.52)
    m2_result = bind_m2_long_option(chain, market=market, policy=POLICY)
    assert m2_result.binding.eligible is True
    assert m2_result.binding.selected_symbols == ("NIFTY_24000_CE",)
    assert m2_result.binding.strategy_id == "positional_long_option"

    # M1 selects moderately OTM (delta 0.30)
    m1_result = bind_m1_cas_option(chain, market=market, policy=POLICY)
    assert m1_result.binding.eligible is True
    assert m1_result.binding.selected_symbols == ("NIFTY_24300_CE",)
    assert m1_result.binding.strategy_id == "cas_microstructure"

    # Strikes must be distinct
    assert m2_result.binding.selected_symbols != m1_result.binding.selected_symbols


def test_section_3_3_binder_rejects_symbols_absent_from_master() -> None:
    """CRITERIA §3.3: No binder emits a symbol that is absent from the instrument master."""
    market = _market(TrendState.UP, calculated_at=CALCULATED_AT)
    c1 = _option(
        "UNLISTED_GUESSED_SYM",
        strike="24000",
        delta="0.52",
        expiry=date(2026, 10, 1),
        dte=7,
    )

    # Master contains only legitimate exchange symbols; unlisted candidate is rejected
    master_catalog = frozenset({"NIFTY26OCT24000CE", "NIFTY26OCT24100CE"})

    # M2 rejects
    bound_m2 = bind_m2_long_option(
        (c1,), market=market, policy=POLICY, master_symbols=master_catalog
    )
    assert bound_m2.binding.eligible is False
    assert bound_m2.binding.reason_codes == (ReasonCode.INSTRUMENT_MASTER_ABSENT,)
    assert bound_m2.binding.selected_symbols == ()

    # M1 rejects
    bound_m1 = bind_m1_cas_option(
        (c1,), market=market, policy=POLICY, master_symbols=master_catalog
    )
    assert bound_m1.binding.eligible is False
    assert bound_m1.binding.reason_codes == (ReasonCode.INSTRUMENT_MASTER_ABSENT,)
    assert bound_m1.binding.selected_symbols == ()
