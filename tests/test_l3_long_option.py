"""Layer 3 first vertical slice: positional long call / long put.

Asserts the boundary: the strategy emits a ratio-based ``TradeIntent`` (never a
quantity), is deterministic, has no lookahead, honors a bounded macro read, and
validates snapshot freshness and option eligibility.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

from tests.factories import (
    NOW,
    index_contract,
    money,
    option_contract,
    portfolio_view,
    price,
    quality,
    quote,
    risk_decision,
    snapshot,
)
from trading.domain.contracts import DerivativesContext, FeatureSnapshot, TradeIntent
from trading.domain.enums import (
    DataQuality,
    OptionType,
    ReasonCode,
    RiskAction,
    Side,
)
from trading.strategies import (
    LongOptionStrategy,
    MacroAssessment,
    MacroBias,
    StrategyContext,
    build_strategy,
)
from trading.strategies._common import technical_bias

NOW_CTX = NOW + timedelta(seconds=60)


def _underlying(last: str, close: str) -> FeatureSnapshot:
    return snapshot(
        snapshot_id="SNAP-UNDER",
        contract=index_contract(),
        market=quote(last=price(last), close=price(close)),
    )


def _option(
    option_type: OptionType = OptionType.CALL,
    *,
    days_to_expiry: int = 10,
    open_interest: int = 5000,
    bid: str = "118.00",
    ask: str = "118.10",
) -> FeatureSnapshot:
    return snapshot(
        snapshot_id="SNAP-OPT",
        contract=option_contract(option_type=option_type),
        market=quote(bid=price(bid), ask=price(ask), last=price("118.05")),
        derivatives=DerivativesContext(
            days_to_expiry=days_to_expiry,
            open_interest=open_interest,
            option_type=option_type,
            underlying_price=price("24000"),
        ),
    )


def _macro(bias: MacroBias, *, confidence: str = "0.8") -> MacroAssessment:
    return MacroAssessment(
        regime="RISK_OFF",
        directional_bias=bias,
        confidence=Decimal(confidence),
        fresh_until=NOW_CTX + timedelta(hours=1),
        evidence_ids=("src-1", "src-2"),
        model_version="macro-agent-v1",
    )


def _ctx(
    underlying: FeatureSnapshot | None = None,
    option: FeatureSnapshot | None = None,
    *,
    now: Any = NOW_CTX,
    macro: MacroAssessment | None = None,
    view: Any = None,
) -> StrategyContext:
    return StrategyContext(
        underlying=underlying or _underlying("24100", "24000"),
        candidates=(option or _option(),),
        view=view or portfolio_view(),
        now=now,
        macro=macro,
    )


def test_registry_builds_long_option() -> None:
    strategy = build_strategy("positional_long_option")
    assert isinstance(strategy, LongOptionStrategy)
    assert strategy.strategy_version == "long-option-v2"


def test_bullish_technical_emits_long_call() -> None:
    decision = LongOptionStrategy().evaluate(_ctx())
    assert decision.emits_intent
    intent = decision.intents[0]
    assert intent.legs[0].side is Side.BUY
    assert intent.legs[0].ratio == 1
    assert intent.legs[0].contract.option_type is OptionType.CALL
    assert intent.setup_code == "LONG_CALL_BULLISH"
    assert intent.requested_risk == intent.estimated_max_loss == money("10000")


def test_bearish_technical_emits_long_put() -> None:
    ctx = _ctx(
        underlying=_underlying("23900", "24000"),
        option=_option(OptionType.PUT),
    )
    decision = LongOptionStrategy().evaluate(ctx)
    assert decision.emits_intent
    assert decision.intents[0].legs[0].contract.option_type is OptionType.PUT
    assert decision.intents[0].setup_code == "LONG_PUT_BEARISH"


def test_neutral_technical_emits_nothing() -> None:
    ctx = _ctx(underlying=_underlying("24000", "24000"))
    decision = LongOptionStrategy().evaluate(ctx)
    assert not decision.emits_intent
    assert decision.rejections == ()


def test_fresh_confident_macro_overrides_technical() -> None:
    # Underlying is technically bullish (last > close), but a fresh bearish macro
    # read is the bounded, higher-authority input.
    ctx = _ctx(
        underlying=_underlying("24100", "24000"),
        option=_option(OptionType.PUT),
        macro=_macro(MacroBias.BEARISH),
    )
    decision = LongOptionStrategy().evaluate(ctx)
    assert decision.emits_intent
    intent = decision.intents[0]
    assert intent.legs[0].contract.option_type is OptionType.PUT
    assert intent.strategy_confidence == Decimal("0.8")


def test_sub_threshold_price_move_abstains_as_noise() -> None:
    underlying = _underlying("24001", "24000")
    assert technical_bias(underlying) is MacroBias.NEUTRAL
    decision = LongOptionStrategy().evaluate(_ctx(underlying=underlying))
    assert not decision.emits_intent


def test_stale_macro_falls_back_to_technical() -> None:
    macro = _macro(MacroBias.BEARISH).model_copy(
        update={"fresh_until": NOW_CTX - timedelta(seconds=1)}
    )
    ctx = _ctx(underlying=_underlying("24100", "24000"), macro=macro)
    decision = LongOptionStrategy().evaluate(ctx)
    assert decision.intents[0].legs[0].contract.option_type is OptionType.CALL


def test_low_confidence_macro_is_ignored() -> None:
    ctx = _ctx(
        underlying=_underlying("24100", "24000"),
        macro=_macro(MacroBias.BEARISH, confidence="0.4"),
    )
    decision = LongOptionStrategy().evaluate(ctx)
    assert decision.intents[0].legs[0].contract.option_type is OptionType.CALL


def test_same_inputs_produce_identical_intent_id() -> None:
    first = LongOptionStrategy().evaluate(_ctx()).intents[0]
    second = LongOptionStrategy().evaluate(_ctx()).intents[0]
    assert first.intent_id == second.intent_id
    assert first == second


def test_option_type_mismatch_skips_candidate() -> None:
    # Bullish read but the candidate is a PUT: skip, do not reject.
    ctx = _ctx(underlying=_underlying("24100", "24000"), option=_option(OptionType.PUT))
    decision = LongOptionStrategy().evaluate(ctx)
    assert not decision.emits_intent
    assert decision.rejections == ()


def test_stale_underlying_blocks_entry() -> None:
    underlying = _underlying("24100", "24000").model_copy(
        update={
            "quality": quality(
                state=DataQuality.STALE, reason_codes=(ReasonCode.DATA_STALE,)
            )
        }
    )
    decision = LongOptionStrategy().evaluate(_ctx(underlying=underlying))
    assert not decision.emits_intent
    assert decision.rejections[0].reason is ReasonCode.DATA_INVALID


def test_snapshot_age_past_limit_blocks_entry() -> None:
    decision = LongOptionStrategy().evaluate(_ctx(now=NOW + timedelta(minutes=5)))
    assert not decision.emits_intent
    assert decision.rejections[0].reason is ReasonCode.DATA_STALE


def test_decision_before_calculation_time_is_rejected() -> None:
    # No lookahead: the decision instant must not precede the snapshot.
    decision = LongOptionStrategy().evaluate(_ctx(now=NOW - timedelta(seconds=1)))
    assert not decision.emits_intent
    assert decision.rejections[0].reason is ReasonCode.DATA_INVALID


def test_zero_or_one_dte_option_is_excluded() -> None:
    ctx = _ctx(option=_option(days_to_expiry=1))
    decision = LongOptionStrategy().evaluate(ctx)
    assert not decision.emits_intent
    assert decision.rejections[0].reason is ReasonCode.EXPIRY_0_1_DTE_EXCLUDED


def test_following_week_dte_is_eligible() -> None:
    decision = LongOptionStrategy().evaluate(_ctx(option=_option(days_to_expiry=4)))
    assert decision.emits_intent
    template = decision.intents[0].exit_template
    assert template.trailing_activation_ticks == 40
    assert template.trailing_distance_ticks == 20
    assert template.time_exit is not None


def test_low_open_interest_is_ineligible() -> None:
    ctx = _ctx(option=_option(open_interest=50))
    decision = LongOptionStrategy().evaluate(ctx)
    assert not decision.emits_intent
    assert decision.rejections[0].reason is ReasonCode.DEPTH_INSUFFICIENT


def test_missing_open_interest_is_ineligible() -> None:
    original = _option()
    assert original.derivatives is not None
    option = original.model_copy(
        update={
            "derivatives": original.derivatives.model_copy(
                update={"open_interest": None}
            )
        }
    )
    decision = LongOptionStrategy().evaluate(_ctx(option=option))
    assert not decision.emits_intent
    assert decision.rejections[0].reason is ReasonCode.DEPTH_INSUFFICIENT


def test_wide_spread_is_ineligible() -> None:
    ctx = _ctx(option=_option(bid="100.00", ask="110.00"))
    decision = LongOptionStrategy().evaluate(ctx)
    assert not decision.emits_intent
    assert decision.rejections[0].reason is ReasonCode.SPREAD_TOO_WIDE


def test_entries_not_permitted_blocks_intent() -> None:
    view = portfolio_view(entries_permitted=False)
    decision = LongOptionStrategy().evaluate(_ctx(view=view))
    assert not decision.emits_intent
    assert decision.rejections[0].reason is ReasonCode.ENTRY_FROZEN


def test_layer3_emits_ratio_and_layer2_adds_quantity() -> None:
    """Ownership boundary: Layer 3 emits a ratio leg; Layer 2 alone sizes lots."""
    intent: TradeIntent = LongOptionStrategy().evaluate(_ctx()).intents[0]
    assert intent.legs[0].ratio == 1

    # A mock Layer 2 rejects the intent; Layer 3 has no path to override it.
    rejected = risk_decision(
        intent_id=intent.intent_id,
        correlation_id=intent.correlation_id,
        action=RiskAction.REJECT,
        approved_legs=(),
        capital_reservation_id=None,
        reserved_capital=None,
        recalculated_max_loss=None,
        margin_required=None,
        post_trade_projection=None,
        reason_codes=(ReasonCode.RISK_LIMIT_TRADE,),
    )
    assert not rejected.permits_submission
