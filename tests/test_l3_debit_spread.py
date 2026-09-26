"""Layer 3 slice 2: vertical debit spread (bull call / bear put)."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

from tests.factories import (
    NOW,
    index_contract,
    option_contract,
    portfolio_view,
    price,
    quote,
    snapshot,
)
from trading.domain.contracts import DerivativesContext, FeatureSnapshot, IntentLeg
from trading.domain.enums import OptionType, ReasonCode, Side
from trading.strategies import (
    DebitSpreadStrategy,
    MacroAssessment,
    MacroBias,
    StrategyContext,
)

NOW_CTX = NOW + timedelta(seconds=60)


def _underlying(last: str, close: str) -> FeatureSnapshot:
    return snapshot(
        snapshot_id="SNAP-UNDER",
        contract=index_contract(),
        market=quote(last=price(last), close=price(close)),
    )


def _option(option_type: OptionType, strike: str) -> FeatureSnapshot:
    return snapshot(
        snapshot_id=f"SNAP-{option_type.value}-{strike}",
        contract=option_contract(
            option_type=option_type,
            strike=Decimal(strike),
            symbol=f"NIFTY26SEP{strike}{option_type.value}",
        ),
        market=quote(bid=price("118.00"), ask=price("118.10"), last=price("118.05")),
        derivatives=DerivativesContext(
            days_to_expiry=10,
            open_interest=5000,
            option_type=option_type,
            underlying_price=price("24000"),
        ),
    )


def _ctx(candidates: tuple[FeatureSnapshot, ...], **overrides: Any) -> StrategyContext:
    return StrategyContext(
        underlying=overrides.get("underlying") or _underlying("24100", "24000"),
        candidates=candidates,
        view=overrides.get("view") or portfolio_view(),
        now=overrides.get("now", NOW_CTX),
        macro=overrides.get("macro"),
    )


def _bull_call() -> tuple[FeatureSnapshot, FeatureSnapshot]:
    return _option(OptionType.CALL, "24000"), _option(OptionType.CALL, "24100")


def _bear_put() -> tuple[FeatureSnapshot, FeatureSnapshot]:
    return _option(OptionType.PUT, "24000"), _option(OptionType.PUT, "23900")


def _strike_of(leg: IntentLeg) -> Decimal:
    strike = leg.contract.strike
    assert strike is not None
    return strike


def test_bullish_emits_bull_call_spread() -> None:
    decision = DebitSpreadStrategy().evaluate(_ctx(_bull_call()))
    assert decision.emits_intent
    legs = decision.intents[0].legs
    assert [leg.side for leg in legs] == [Side.BUY, Side.SELL]
    assert _strike_of(legs[0]) < _strike_of(legs[1])
    assert decision.intents[0].setup_code == "BULL_CALL_SPREAD"


def test_bearish_emits_bear_put_spread() -> None:
    ctx = _ctx(_bear_put(), underlying=_underlying("23900", "24000"))
    decision = DebitSpreadStrategy().evaluate(ctx)
    assert decision.emits_intent
    legs = decision.intents[0].legs
    assert legs[0].side is Side.BUY
    assert _strike_of(legs[0]) > _strike_of(legs[1])
    assert decision.intents[0].setup_code == "BEAR_PUT_SPREAD"


def test_mixed_option_types_are_rejected() -> None:
    candidates = (_option(OptionType.CALL, "24000"), _option(OptionType.PUT, "23900"))
    decision = DebitSpreadStrategy().evaluate(_ctx(candidates))
    assert not decision.emits_intent
    assert decision.rejections[0].reason is ReasonCode.INSTRUMENT_UNKNOWN


def test_wrong_candidate_count_is_rejected() -> None:
    decision = DebitSpreadStrategy().evaluate(
        _ctx((_option(OptionType.CALL, "24000"),))
    )
    assert not decision.emits_intent
    assert decision.rejections[0].reason is ReasonCode.INSTRUMENT_UNKNOWN


def test_neutral_emits_nothing() -> None:
    ctx = _ctx(_bull_call(), underlying=_underlying("24000", "24000"))
    decision = DebitSpreadStrategy().evaluate(ctx)
    assert not decision.emits_intent
    assert decision.rejections[0].reason is ReasonCode.DIRECTION_NEUTRAL


def test_deterministic_intent_id() -> None:
    first = DebitSpreadStrategy().evaluate(_ctx(_bull_call())).intents[0]
    second = DebitSpreadStrategy().evaluate(_ctx(_bull_call())).intents[0]
    assert first.intent_id == second.intent_id


def test_two_legs_carry_ratio_not_quantity() -> None:
    decision = DebitSpreadStrategy().evaluate(_ctx(_bull_call()))
    assert all(leg.ratio == 1 for leg in decision.intents[0].legs)


def test_bearish_macro_overrides_technical() -> None:
    macro = MacroAssessment(
        regime="RISK_OFF",
        directional_bias=MacroBias.BEARISH,
        confidence=Decimal("0.8"),
        fresh_until=NOW_CTX + timedelta(hours=1),
        evidence_ids=("src-1",),
        model_version="macro-agent-v1",
    )
    # Underlying is technically bullish, but the fresh bearish macro wins.
    ctx = _ctx(_bear_put(), underlying=_underlying("24100", "24000"), macro=macro)
    decision = DebitSpreadStrategy().evaluate(ctx)
    assert decision.emits_intent
    assert decision.intents[0].setup_code == "BEAR_PUT_SPREAD"


def test_candidates_are_always_the_type_the_read_requires() -> None:
    """Regression: a resolved direction must pick the matching option legs.

    A bullish read over put candidates previously produced a "bull call spread"
    built from puts, which is a bear put spread — the emitted position was
    directionally inverted against the signal it came from.
    """
    decision = DebitSpreadStrategy().evaluate(_ctx(_bear_put()))
    assert not decision.emits_intent
    assert decision.rejections[0].reason is ReasonCode.OPTION_TYPE_MISMATCH


def test_bearish_read_over_call_candidates_emits_nothing() -> None:
    """The converse of the regression: bearish read, call candidates."""
    ctx = _ctx(_bull_call(), underlying=_underlying("23900", "24000"))
    decision = DebitSpreadStrategy().evaluate(ctx)
    assert not decision.emits_intent
    assert decision.rejections[0].reason is ReasonCode.OPTION_TYPE_MISMATCH


def test_emitted_legs_always_match_the_setup_code() -> None:
    """Every emitted spread names a structure whose legs really express it."""
    bullish = DebitSpreadStrategy().evaluate(_ctx(_bull_call()))
    assert bullish.emits_intent
    assert bullish.intents[0].setup_code == "BULL_CALL_SPREAD"
    assert all(
        leg.contract.option_type is OptionType.CALL for leg in bullish.intents[0].legs
    )

    bearish = DebitSpreadStrategy().evaluate(
        _ctx(_bear_put(), underlying=_underlying("23900", "24000"))
    )
    assert bearish.emits_intent
    assert bearish.intents[0].setup_code == "BEAR_PUT_SPREAD"
    assert all(
        leg.contract.option_type is OptionType.PUT for leg in bearish.intents[0].legs
    )
