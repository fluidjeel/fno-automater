"""Layer 3 slice 5: defined-risk multi-leg credit structures."""

from __future__ import annotations

from datetime import date, timedelta
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
from trading.domain.contracts import (
    DerivativesContext,
    FeatureSnapshot,
    IntentLeg,
    TradeIntent,
)
from trading.domain.enums import OptionType, ReasonCode, Side
from trading.strategies import (
    MacroAssessment,
    MacroBias,
    MultiLegOptionsStrategy,
    StrategyContext,
)

NOW_CTX = NOW + timedelta(seconds=60)


def _underlying(last: str, close: str) -> FeatureSnapshot:
    return snapshot(
        snapshot_id="SNAP-UNDER",
        contract=index_contract(),
        market=quote(last=price(last), close=price(close)),
    )


def _option(
    option_type: OptionType,
    strike: str,
    days_to_expiry: int = 10,
    open_interest: int = 5000,
) -> FeatureSnapshot:
    return snapshot(
        snapshot_id=f"SNAP-{option_type.value}-{strike}",
        contract=option_contract(
            option_type=option_type,
            strike=Decimal(strike),
            symbol=f"NIFTY26SEP{strike}{option_type.value}",
        ),
        market=quote(bid=price("118.00"), ask=price("118.10"), last=price("118.05")),
        derivatives=DerivativesContext(
            days_to_expiry=days_to_expiry,
            open_interest=open_interest,
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


def _bull_put() -> tuple[FeatureSnapshot, FeatureSnapshot]:
    """Sell the 24000 put, buy the 23900 put as protection."""
    return _option(OptionType.PUT, "23900"), _option(OptionType.PUT, "24000")


def _bear_call() -> tuple[FeatureSnapshot, FeatureSnapshot]:
    """Sell the 24000 call, buy the 24100 call as protection."""
    return _option(OptionType.CALL, "24000"), _option(OptionType.CALL, "24100")


def _strike_of(leg: IntentLeg) -> Decimal:
    strike = leg.contract.strike
    assert strike is not None
    return strike


def _intent(ctx: StrategyContext) -> TradeIntent:
    decision = MultiLegOptionsStrategy().evaluate(ctx)
    assert decision.emits_intent
    return decision.intents[0]


def test_bullish_emits_bull_put_spread() -> None:
    intent = _intent(_ctx(_bull_put()))
    assert intent.setup_code == "BULL_PUT_SPREAD"
    assert [leg.side for leg in intent.legs] == [Side.BUY, Side.SELL]
    assert all(leg.contract.option_type is OptionType.PUT for leg in intent.legs)


def test_bearish_emits_bear_call_spread() -> None:
    ctx = _ctx(_bear_call(), underlying=_underlying("23900", "24000"))
    intent = _intent(ctx)
    assert intent.setup_code == "BEAR_CALL_SPREAD"
    assert [leg.side for leg in intent.legs] == [Side.SELL, Side.BUY]
    assert all(leg.contract.option_type is OptionType.CALL for leg in intent.legs)


def test_legs_are_ordered_by_strike() -> None:
    for ctx in (
        _ctx(_bull_put()),
        _ctx(_bear_call(), underlying=_underlying("23900", "24000")),
    ):
        legs = _intent(ctx).legs
        assert _strike_of(legs[0]) < _strike_of(legs[1])


def test_short_leg_is_never_naked() -> None:
    """Every short leg is covered by a long wing further out of the money."""
    bull = _intent(_ctx(_bull_put())).legs
    assert sum(1 for leg in bull if leg.side is Side.SELL) == 1
    bull_long = next(leg for leg in bull if leg.side is Side.BUY)
    bull_short = next(leg for leg in bull if leg.side is Side.SELL)
    assert _strike_of(bull_long) < _strike_of(bull_short)

    bear = _intent(_ctx(_bear_call(), underlying=_underlying("23900", "24000"))).legs
    bear_long = next(leg for leg in bear if leg.side is Side.BUY)
    bear_short = next(leg for leg in bear if leg.side is Side.SELL)
    assert _strike_of(bear_long) > _strike_of(bear_short)


def test_max_loss_is_the_requested_risk_and_fills_are_all_or_cancel() -> None:
    intent = _intent(_ctx(_bull_put()))
    assert intent.estimated_max_loss == intent.requested_risk
    assert intent.exit_template.partial_fill_policy == "ALL_OR_CANCEL"


def test_neutral_emits_nothing() -> None:
    decision = MultiLegOptionsStrategy().evaluate(
        _ctx(_bull_put(), underlying=_underlying("24000", "24000"))
    )
    assert not decision.emits_intent
    assert decision.rejections == ()


def test_option_type_mismatch_emits_nothing() -> None:
    # Bullish read, but the caller supplied calls instead of puts.
    decision = MultiLegOptionsStrategy().evaluate(_ctx(_bear_call()))
    assert not decision.emits_intent
    assert decision.rejections == ()


def test_mixed_option_types_are_rejected() -> None:
    candidates = (_option(OptionType.PUT, "23900"), _option(OptionType.CALL, "24000"))
    decision = MultiLegOptionsStrategy().evaluate(_ctx(candidates))
    assert not decision.emits_intent
    assert decision.rejections[0].reason is ReasonCode.INSTRUMENT_UNKNOWN


def test_mixed_expiries_are_rejected() -> None:
    candidates = (
        _option(OptionType.PUT, "23900"),
        snapshot(
            snapshot_id="SNAP-FAR",
            contract=option_contract(
                option_type=OptionType.PUT,
                strike=Decimal("24000"),
                expiry=date(2026, 10, 29),
            ),
            market=quote(bid=price("118.00"), ask=price("118.10")),
            derivatives=DerivativesContext(days_to_expiry=45, open_interest=5000),
        ),
    )
    decision = MultiLegOptionsStrategy().evaluate(_ctx(candidates))
    assert not decision.emits_intent
    assert decision.rejections[0].reason is ReasonCode.INSTRUMENT_UNKNOWN


def test_wrong_candidate_count_is_rejected() -> None:
    decision = MultiLegOptionsStrategy().evaluate(
        _ctx((_option(OptionType.PUT, "23900"),))
    )
    assert not decision.emits_intent
    assert decision.rejections[0].reason is ReasonCode.INSTRUMENT_UNKNOWN


def test_expiring_leg_is_rejected() -> None:
    candidates = (
        _option(OptionType.PUT, "23900", days_to_expiry=0),
        _option(OptionType.PUT, "24000"),
    )
    decision = MultiLegOptionsStrategy().evaluate(_ctx(candidates))
    assert not decision.emits_intent
    assert decision.rejections[0].reason is ReasonCode.CONTRACT_EXPIRED


def test_thin_leg_is_rejected() -> None:
    candidates = (
        _option(OptionType.PUT, "23900", open_interest=10),
        _option(OptionType.PUT, "24000"),
    )
    decision = MultiLegOptionsStrategy().evaluate(_ctx(candidates))
    assert not decision.emits_intent
    assert decision.rejections[0].reason is ReasonCode.DEPTH_INSUFFICIENT


def test_stale_snapshot_is_rejected() -> None:
    decision = MultiLegOptionsStrategy().evaluate(
        _ctx(_bull_put(), now=NOW_CTX + timedelta(seconds=300))
    )
    assert not decision.emits_intent
    assert decision.rejections[0].reason is ReasonCode.DATA_STALE


def test_frozen_entries_are_rejected() -> None:
    view = portfolio_view(entries_permitted=False)
    decision = MultiLegOptionsStrategy().evaluate(_ctx(_bull_put(), view=view))
    assert not decision.emits_intent
    assert decision.rejections[0].reason is ReasonCode.ENTRY_FROZEN


def test_intent_id_is_deterministic() -> None:
    first = _intent(_ctx(_bull_put()))
    second = _intent(_ctx(_bull_put()))
    assert first.intent_id == second.intent_id
    assert first.correlation_id == second.correlation_id


def test_legs_carry_ratio_not_quantity() -> None:
    intent = _intent(_ctx(_bull_put()))
    assert all(leg.ratio == 1 for leg in intent.legs)
    assert not hasattr(intent.legs[0], "quantity")


def test_leg_ids_are_unique_and_role_based() -> None:
    intent = _intent(_ctx(_bull_put()))
    assert {leg.leg_id for leg in intent.legs} == {"leg-long", "leg-short"}


def test_fresh_bearish_macro_overrides_bullish_technical() -> None:
    macro = MacroAssessment(
        regime="RISK_OFF",
        directional_bias=MacroBias.BEARISH,
        confidence=Decimal("0.8"),
        fresh_until=NOW_CTX + timedelta(hours=1),
        evidence_ids=("src-1",),
        model_version="macro-agent-v1",
    )
    ctx = _ctx(_bear_call(), underlying=_underlying("24100", "24000"), macro=macro)
    assert _intent(ctx).setup_code == "BEAR_CALL_SPREAD"


def test_stale_macro_falls_back_to_technical() -> None:
    macro = MacroAssessment(
        regime="RISK_OFF",
        directional_bias=MacroBias.BEARISH,
        confidence=Decimal("0.8"),
        fresh_until=NOW_CTX - timedelta(seconds=1),
        evidence_ids=("src-1",),
        model_version="macro-agent-v1",
    )
    ctx = _ctx(_bull_put(), macro=macro)
    assert _intent(ctx).setup_code == "BULL_PUT_SPREAD"


def test_low_confidence_macro_falls_back_to_technical() -> None:
    macro = MacroAssessment(
        regime="RISK_OFF",
        directional_bias=MacroBias.BEARISH,
        confidence=Decimal("0.30"),
        fresh_until=NOW_CTX + timedelta(hours=1),
        evidence_ids=("src-1",),
        model_version="macro-agent-v1",
    )
    ctx = _ctx(_bull_put(), macro=macro)
    assert _intent(ctx).setup_code == "BULL_PUT_SPREAD"


def test_macro_never_bypasses_a_rejection() -> None:
    macro = MacroAssessment(
        regime="RISK_ON",
        directional_bias=MacroBias.BULLISH,
        confidence=Decimal("0.9"),
        fresh_until=NOW_CTX + timedelta(hours=1),
        evidence_ids=("src-1",),
        model_version="macro-agent-v1",
    )
    ctx = _ctx(_bull_put(), now=NOW_CTX + timedelta(seconds=300), macro=macro)
    decision = MultiLegOptionsStrategy().evaluate(ctx)
    assert not decision.emits_intent
    assert decision.rejections[0].reason is ReasonCode.DATA_STALE
