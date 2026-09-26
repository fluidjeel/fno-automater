"""Tests for Layer 3 Iron Condor strategy."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from tests.factories import (
    NOW,
    index_contract,
    option_contract,
    portfolio_view,
    snapshot,
    snapshot_times,
)
from trading.domain.contracts import FeatureSnapshot
from trading.domain.contracts.snapshot import DerivativesContext
from trading.domain.enums import Exchange, InstrumentKind, OptionType, ReasonCode
from trading.domain.primitives import Price, TickSize
from trading.risk.sizing.iron_condor import condor_legs, is_iron_condor
from trading.strategies.base import StrategyContext
from trading.strategies.iron_condor import IronCondorStrategy
from trading.strategies.macro import MacroAssessment, MacroBias

EXPIRY = date(2026, 9, 24)
NOW_CTX = NOW + timedelta(seconds=60)


def _opt_snap(symbol: str, strike: str, opt_type: OptionType) -> FeatureSnapshot:
    contract = option_contract(
        symbol=symbol,
        exchange=Exchange.NFO,
        instrument_kind=InstrumentKind.OPTION,
        underlying="NIFTY",
        expiry=EXPIRY,
        strike=Decimal(strike),
        option_type=opt_type,
    )
    return snapshot(
        contract=contract,
        times=snapshot_times(),
        derivatives=DerivativesContext(
            days_to_expiry=10,
            open_interest=5000,
            option_type=opt_type,
            underlying_price=Price.snap(Decimal("24100"), TickSize(Decimal("0.05"))),
        ),
    )


def _macro(bias: MacroBias, confidence: str = "0.8") -> MacroAssessment:
    return MacroAssessment(
        regime="NEUTRAL_VOLATILITY",
        directional_bias=bias,
        confidence=Decimal(confidence),
        fresh_until=NOW + timedelta(hours=1),
        evidence_ids=("src-1",),
        model_version="macro-v1",
    )


def test_iron_condor_regime_rejection() -> None:
    strat = IronCondorStrategy()
    underlying = snapshot(
        contract=index_contract(symbol="NIFTY", underlying="NIFTY"),
        times=snapshot_times(),
    )
    ctx_bullish = StrategyContext(
        underlying=underlying,
        candidates=(),
        view=portfolio_view(),
        now=NOW_CTX,
        macro=_macro(MacroBias.BULLISH),
    )

    dec = strat.evaluate(ctx_bullish)
    assert dec.emits_intent is False
    assert len(dec.rejections) == 1
    assert dec.rejections[0].reason is ReasonCode.DATA_INVALID


def test_iron_condor_unequal_wings_rejection() -> None:
    strat = IronCondorStrategy()
    underlying = snapshot(
        contract=index_contract(symbol="NIFTY", underlying="NIFTY"),
        times=snapshot_times(),
    )

    # Put wing: 24000 (long) to 24200 (short) -> width 200
    lp = _opt_snap("NIFTY2692424000PE", "24000", OptionType.PUT)
    sp = _opt_snap("NIFTY2692424200PE", "24200", OptionType.PUT)

    # Call wing: 24800 (short) to 24900 (long) -> width 100 (mismatch!)
    sc = _opt_snap("NIFTY2692424800CE", "24800", OptionType.CALL)
    lc = _opt_snap("NIFTY2692424900CE", "24900", OptionType.CALL)

    ctx = StrategyContext(
        underlying=underlying,
        candidates=(lp, sp, sc, lc),
        view=portfolio_view(),
        now=NOW_CTX,
        macro=_macro(MacroBias.NEUTRAL),
    )

    dec = strat.evaluate(ctx)
    assert dec.emits_intent is False
    assert len(dec.rejections) == 1
    assert "unequal wing widths" in dec.rejections[0].detail


def test_iron_condor_approval_and_l2_compliance() -> None:
    strat = IronCondorStrategy()
    underlying = snapshot(
        contract=index_contract(symbol="NIFTY", underlying="NIFTY"),
        times=snapshot_times(),
    )

    # Put wing: 24000 to 24200 (width 200)
    lp = _opt_snap("NIFTY2692424000PE", "24000", OptionType.PUT)
    sp = _opt_snap("NIFTY2692424200PE", "24200", OptionType.PUT)

    # Call wing: 24800 to 25000 (width 200)
    sc = _opt_snap("NIFTY2692424800CE", "24800", OptionType.CALL)
    lc = _opt_snap("NIFTY2692425000CE", "25000", OptionType.CALL)

    ctx = StrategyContext(
        underlying=underlying,
        candidates=(lp, sp, sc, lc),
        view=portfolio_view(),
        now=NOW_CTX,
        macro=_macro(MacroBias.NEUTRAL),
    )

    dec = strat.evaluate(ctx)
    assert dec.emits_intent is True
    assert len(dec.intents) == 1
    intent = dec.intents[0]
    assert len(intent.legs) == 4
    assert intent.setup_code == "IRON_CONDOR"

    # Verify that Layer 2 iron condor sizing engine recognizes this intent as valid!
    assert is_iron_condor(intent) is True
    legs = condor_legs(intent)
    assert legs.long_put.contract.strike == Decimal("24000")
    assert legs.short_put.contract.strike == Decimal("24200")
    assert legs.short_call.contract.strike == Decimal("24800")
    assert legs.long_call.contract.strike == Decimal("25000")
