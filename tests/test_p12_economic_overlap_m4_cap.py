"""Phase P12 Economic Overlap, Conflict Policy, M4 Cap, and Counterfactual Book.

Locks in Phase P12 requirements from:
- FOUR_MODE_LAYER_CHANGE_CRITERIA.md (§2.7, §4.3, §4.9)
- NIFTY_FOUR_MODE_CURSOR_REDESIGN.md (§11, T26, T28, T30, T49)
- FOUR_MODE_REDESIGN_PLAN.md (Phase P12)
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import tests.factories as f
from trading.domain.clock import FrozenClock
from trading.domain.contracts import DerivativesContext, Greeks, IntentLeg
from trading.domain.enums import (
    FamilyId,
    ModeId,
    OptionType,
    ReasonCode,
    Side,
)
from trading.domain.primitives import Currency, Money
from trading.portfolio.arbitration import PortfolioArbiter
from trading.portfolio.campaign_drawdown import CampaignDrawdownLedger
from trading.portfolio.counterfactual_book import build_mode_books
from trading.portfolio.economic_overlap import (
    MAX_M4_OPEN_POSITIONS,
    ThesisDirection,
    economic_keys_overlap,
    extract_economic_exposure,
)
from trading.storage.trading_store import TradingStore

NOW = f.NOW
EXPIRY = date(2026, 10, 1)


def _option_snap(
    symbol: str,
    strike: str,
    option_type: OptionType,
) -> object:
    return f.snapshot(
        contract=f.option_contract(
            symbol=symbol,
            strike=Decimal(strike),
            option_type=option_type,
            expiry=EXPIRY,
        ),
        derivatives=DerivativesContext(
            days_to_expiry=10,
            open_interest=5000,
            option_type=option_type,
            greeks=Greeks(
                model="fixture",
                calculation_version="1",
                converged=True,
                implied_volatility=Decimal("15"),
                delta=Decimal("0.30"),
            ),
            underlying_price=f.price("24500"),
        ),
    )


def _vertical_intent(
    *,
    intent_id: str,
    family_id: FamilyId,
    mode_id: ModeId,
    low_strike: str,
    high_strike: str,
    option_type: OptionType,
    low_side: Side,
    high_side: Side,
    confidence: str = "0.80",
) -> object:
    suffix = "CE" if option_type is OptionType.CALL else "PE"
    low = _option_snap(f"NIFTY26OCT{low_strike}{suffix}", low_strike, option_type)
    high = _option_snap(f"NIFTY26OCT{high_strike}{suffix}", high_strike, option_type)
    return f.intent(
        intent_id=intent_id,
        mode_id=mode_id,
        family_id=family_id.value,
        legs=(
            IntentLeg(leg_id="leg-low", contract=low.contract, side=low_side, ratio=1),
            IntentLeg(
                leg_id="leg-high", contract=high.contract, side=high_side, ratio=1
            ),
        ),
        strategy_confidence=Decimal(confidence),
        requested_risk=Money.of("2000", Currency.INR),
        estimated_max_loss=Money.of("2000", Currency.INR),
        created_at=NOW,
    )


def _bull_call_debit(intent_id: str, confidence: str = "0.85") -> object:
    return _vertical_intent(
        intent_id=intent_id,
        family_id=FamilyId.bull_call_debit,
        mode_id=ModeId.M3_TACTICAL_POSITIONAL,
        low_strike="24000",
        high_strike="24200",
        option_type=OptionType.CALL,
        low_side=Side.BUY,
        high_side=Side.SELL,
        confidence=confidence,
    )


def _bull_put_credit(intent_id: str, confidence: str = "0.75") -> object:
    return _vertical_intent(
        intent_id=intent_id,
        family_id=FamilyId.bull_put_credit,
        mode_id=ModeId.M4_STRATEGIC_POSITIONAL,
        low_strike="24000",
        high_strike="24200",
        option_type=OptionType.PUT,
        low_side=Side.BUY,
        high_side=Side.SELL,
        confidence=confidence,
    )


def _bear_put_debit(intent_id: str) -> object:
    return _vertical_intent(
        intent_id=intent_id,
        family_id=FamilyId.bear_put_debit,
        mode_id=ModeId.M3_TACTICAL_POSITIONAL,
        low_strike="24000",
        high_strike="24200",
        option_type=OptionType.PUT,
        low_side=Side.SELL,
        high_side=Side.BUY,
        confidence="0.80",
    )


def _m4_condor(intent_id: str, wing_shift: int = 0) -> object:
    legs = []
    for symbol, strike, side, option_type in (
        (
            f"NIFTY26OCT{23800 + wing_shift}PE",
            str(23800 + wing_shift),
            Side.BUY,
            OptionType.PUT,
        ),
        (
            f"NIFTY26OCT{24200 + wing_shift}PE",
            str(24200 + wing_shift),
            Side.SELL,
            OptionType.PUT,
        ),
        (
            f"NIFTY26OCT{24800 + wing_shift}CE",
            str(24800 + wing_shift),
            Side.SELL,
            OptionType.CALL,
        ),
        (
            f"NIFTY26OCT{25200 + wing_shift}CE",
            str(25200 + wing_shift),
            Side.BUY,
            OptionType.CALL,
        ),
    ):
        snap = _option_snap(symbol, strike, option_type)
        legs.append(
            IntentLeg(
                leg_id=f"leg-{symbol}",
                contract=snap.contract,
                side=side,
                ratio=1,
            )
        )
    return f.intent(
        intent_id=intent_id,
        mode_id=ModeId.M4_STRATEGIC_POSITIONAL,
        family_id=FamilyId.short_iron_condor_defined.value,
        legs=tuple(legs),
        strategy_confidence=Decimal("0.70"),
        requested_risk=Money.of("2500", Currency.INR),
        estimated_max_loss=Money.of("2500", Currency.INR),
        created_at=NOW,
    )


class TestP12EconomicOverlap:
    def test_bull_call_and_bull_put_share_economic_key(self) -> None:
        call = _bull_call_debit("INTENT-BULL-CALL")
        put = _bull_put_credit("INTENT-BULL-PUT")
        call_key = extract_economic_exposure(call)
        put_key = extract_economic_exposure(put)
        assert call_key is not None
        assert put_key is not None
        assert call_key.direction is ThesisDirection.BULLISH
        assert put_key.direction is ThesisDirection.BULLISH
        assert economic_keys_overlap(call_key, put_key)

    def test_scenario_t28_economic_overlap_suppressed(self) -> None:
        arbiter = PortfolioArbiter()
        incumbent = _bull_call_debit("INTENT-BULL-CALL")
        challenger = _bull_put_credit("INTENT-BULL-PUT")
        result = arbiter.arbitrate([incumbent, challenger], now=NOW)
        assert len(result.approved_intents) == 1
        assert result.approved_intents[0].intent_id == "INTENT-BULL-CALL"
        assert len(result.suppressed_intents) == 1
        suppression = result.suppressed_intents[0]
        assert suppression.reason_code is ReasonCode.ECONOMIC_OVERLAP_SUPPRESSED
        assert suppression.incumbent_id == "INTENT-BULL-CALL"
        assert suppression.candidate_intent_id == "INTENT-BULL-PUT"


class TestP12ConflictPolicy:
    def test_scenario_t30_opposing_exposure_rejected(self) -> None:
        arbiter = PortfolioArbiter()
        bullish = _bull_call_debit("INTENT-BULL")
        bearish = _bear_put_debit("INTENT-BEAR")
        result = arbiter.arbitrate([bullish, bearish], now=NOW)
        assert len(result.approved_intents) == 1
        assert result.approved_intents[0].intent_id == "INTENT-BULL"
        assert (
            result.suppressed_intents[0].reason_code
            is ReasonCode.OPPOSING_EXPOSURE_REJECTED
        )

    def test_m2_and_m3_both_bullish_allowed(self) -> None:
        """M1/M2 overlap in same direction is permitted when structures differ."""
        arbiter = PortfolioArbiter()
        m3 = _bull_call_debit("INTENT-M3-BULL")
        call_snap = _option_snap("NIFTY26OCT24000CE", "24000", OptionType.CALL)
        m2_call = f.intent(
            intent_id="INTENT-M2-CALL",
            mode_id=ModeId.M2_DIRECTIONAL,
            family_id=FamilyId.long_call.value,
            legs=(
                IntentLeg(
                    leg_id="leg-call",
                    contract=call_snap.contract,
                    side=Side.BUY,
                    ratio=1,
                ),
            ),
            strategy_confidence=Decimal("0.70"),
            requested_risk=Money.of("3000", Currency.INR),
            estimated_max_loss=Money.of("3000", Currency.INR),
            created_at=NOW,
        )
        result = arbiter.arbitrate([m3, m2_call], now=NOW)
        assert len(result.approved_intents) == 2


class TestP12M4PositionCap:
    def test_scenario_t26_third_m4_candidate_suppressed(self) -> None:
        arbiter = PortfolioArbiter()
        first = _m4_condor("INTENT-M4-1", wing_shift=0)
        second = _m4_condor("INTENT-M4-2", wing_shift=50)
        third = _m4_condor("INTENT-M4-3", wing_shift=100)
        result = arbiter.arbitrate([first, second, third], now=NOW)
        assert len(result.approved_intents) == MAX_M4_OPEN_POSITIONS
        assert len(result.suppressed_intents) == 1
        assert (
            result.suppressed_intents[0].reason_code
            is ReasonCode.M4_POSITION_CAP_REACHED
        )
        assert result.suppressed_intents[0].candidate_intent_id == "INTENT-M4-3"

    def test_two_complementary_m4_positions_allowed(self) -> None:
        arbiter = PortfolioArbiter()
        first = _m4_condor("INTENT-M4-A", wing_shift=0)
        second = _bull_put_credit("INTENT-M4-BULL-PUT")
        result = arbiter.arbitrate([first, second], now=NOW)
        assert len(result.approved_intents) == 2


class TestP12CampaignDrawdown:
    def test_drawdown_survives_new_position_id(self) -> None:
        ledger = CampaignDrawdownLedger()
        first = ledger.record_realized_pnl(
            campaign_id="CAMP-1",
            position_id="TRD-1",
            realized_pnl=Money.of("-500", Currency.INR),
        )
        second = ledger.record_realized_pnl(
            campaign_id="CAMP-1",
            position_id="TRD-2",
            realized_pnl=Money.of("-300", Currency.INR),
        )
        assert first.cumulative_realized_pnl.amount == Decimal("-500")
        assert second.cumulative_realized_pnl.amount == Decimal("-800")
        assert second.position_ids == ("TRD-1", "TRD-2")
        assert ledger.cumulative_realized_pnl("CAMP-1").amount == Decimal("-800")


class TestP12CounterfactualBookIsolation:
    def test_counterfactual_books_do_not_mutate_store(self, tmp_path: Path) -> None:
        clock = FrozenClock(NOW + timedelta(seconds=60))
        store = TradingStore.open(tmp_path / "p12_cf.sqlite", clock=clock)
        reservations_before = len(store.list_reservations())
        positions_before = len(store.list_position_lifecycle())

        arbiter = PortfolioArbiter()
        incumbent = _bull_call_debit("INTENT-CF-BULL-CALL")
        duplicate = _bull_put_credit("INTENT-CF-BULL-PUT")
        result = arbiter.arbitrate([incumbent, duplicate], now=clock.now_utc())

        books = build_mode_books(
            (ModeId.M3_TACTICAL_POSITIONAL, ModeId.M4_STRATEGIC_POSITIONAL)
        )
        for book in books.values():
            book.ingest_arbitration(result, now=clock.now_utc())

        assert len(store.list_reservations()) == reservations_before
        assert len(store.list_position_lifecycle()) == positions_before
        m3_book = books[ModeId.M3_TACTICAL_POSITIONAL]
        m4_book = books[ModeId.M4_STRATEGIC_POSITIONAL]
        assert len(m3_book.entries) == 1
        assert m3_book.entries[0].action == "APPROVED"
        assert len(m4_book.entries) == 1
        assert m4_book.entries[0].action == "SUPPRESSED_ECONOMIC_OVERLAP"
        store.close()
