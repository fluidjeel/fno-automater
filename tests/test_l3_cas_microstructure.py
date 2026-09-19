"""Layer 3 slice 4: close-auction (CAS) microstructure."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from tests.factories import (
    index_contract,
    option_contract,
    portfolio_view,
    price,
    quote,
    snapshot,
    snapshot_times,
)
from trading.domain.contracts import (
    DerivativesContext,
    FeatureSnapshot,
    SnapshotTimes,
    TradeIntent,
)
from trading.domain.enums import OptionType, ReasonCode, Side
from trading.strategies import (
    CasMicrostructureStrategy,
    MacroAssessment,
    MacroBias,
    StrategyContext,
)
from trading.strategies.cas_microstructure import (
    CAS_HOLDING_SECONDS,
    FEATURE_AUCTION_IMBALANCE,
    FEATURE_MICROPRICE_EDGE_BPS,
    FEATURE_QUOTE_INSTABILITY,
    FEATURE_SET_VERSION,
    FEATURE_TRADE_FLOW_IMBALANCE,
    MAX_HOLDING_DAYS,
)

# 2026-09-14 is a Monday; 09:45 UTC is 15:15 IST, inside the closing window.
CAS_NOW = datetime(2026, 9, 14, 9, 45, tzinfo=UTC)

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


def _times(instant: datetime = CAS_NOW) -> SnapshotTimes:
    return snapshot_times(
        event_time=instant - timedelta(seconds=2),
        source_time=instant - timedelta(seconds=1),
        receive_time=instant - timedelta(milliseconds=500),
        calculation_time=instant - timedelta(milliseconds=200),
    )


def _underlying(
    last: str,
    close: str,
    features: dict[str, Decimal] | None = None,
    instant: datetime = CAS_NOW,
) -> FeatureSnapshot:
    return snapshot(
        snapshot_id="SNAP-UNDER",
        contract=index_contract(),
        times=_times(instant),
        market=quote(last=price(last), close=price(close)),
        feature_set_version=FEATURE_SET_VERSION,
        features=BULLISH_FEATURES if features is None else features,
    )


def _option(
    option_type: OptionType,
    days_to_expiry: int = 3,
    open_interest: int = 5000,
    instant: datetime = CAS_NOW,
) -> FeatureSnapshot:
    return snapshot(
        snapshot_id=f"SNAP-{option_type.value}",
        contract=option_contract(
            option_type=option_type,
            symbol=f"NIFTY26SEP24000{option_type.value}",
        ),
        times=_times(instant),
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
        now=overrides.get("now", CAS_NOW),
        macro=overrides.get("macro"),
    )


def test_wrong_feature_set_version_blocks_entry() -> None:
    underlying = _underlying("24100", "24000").model_copy(
        update={"feature_set_version": "different-feature-contract"}
    )
    decision = CasMicrostructureStrategy().evaluate(
        _ctx((_option(OptionType.CALL),), underlying=underlying)
    )
    assert not decision.emits_intent
    assert decision.rejections[0].reason is ReasonCode.DATA_INVALID


def _intent(ctx: StrategyContext) -> TradeIntent:
    decision = CasMicrostructureStrategy().evaluate(ctx)
    assert decision.emits_intent
    return decision.intents[0]


def _macro(
    bias: MacroBias, confidence: str = "0.8", fresh: bool = True
) -> MacroAssessment:
    return MacroAssessment(
        regime="RISK_ON",
        directional_bias=bias,
        confidence=Decimal(confidence),
        fresh_until=CAS_NOW + (timedelta(hours=1) if fresh else timedelta(seconds=-1)),
        evidence_ids=("src-1",),
        model_version="macro-agent-v1",
    )


def test_bullish_emits_long_call() -> None:
    intent = _intent(_ctx((_option(OptionType.CALL),)))
    assert intent.setup_code == "CAS_CALL_BULLISH"
    assert intent.legs[0].side is Side.BUY
    assert intent.legs[0].contract.option_type is OptionType.CALL


def test_bearish_emits_long_put() -> None:
    ctx = _ctx(
        (_option(OptionType.PUT),),
        underlying=_underlying("23900", "24000", BEARISH_FEATURES),
    )
    intent = _intent(ctx)
    assert intent.setup_code == "CAS_PUT_BEARISH"
    assert intent.legs[0].side is Side.BUY
    assert intent.legs[0].contract.option_type is OptionType.PUT


def test_neutral_emits_nothing() -> None:
    decision = CasMicrostructureStrategy().evaluate(
        _ctx((_option(OptionType.CALL),), underlying=_underlying("24000", "24000"))
    )
    assert not decision.emits_intent
    assert decision.rejections == ()


def test_mandatory_time_exit_is_short_and_dated_from_now() -> None:
    intent = _intent(_ctx((_option(OptionType.CALL),)))
    assert intent.exit_template.time_exit == CAS_NOW + timedelta(
        seconds=CAS_HOLDING_SECONDS
    )
    assert intent.constraints.max_holding_days == MAX_HOLDING_DAYS


def test_feature_absence_is_a_gap_not_a_default() -> None:
    for missing in (
        FEATURE_AUCTION_IMBALANCE,
        FEATURE_TRADE_FLOW_IMBALANCE,
        FEATURE_MICROPRICE_EDGE_BPS,
        FEATURE_QUOTE_INSTABILITY,
    ):
        features = {k: v for k, v in BULLISH_FEATURES.items() if k != missing}
        decision = CasMicrostructureStrategy().evaluate(
            _ctx(
                (_option(OptionType.CALL),),
                underlying=_underlying("24100", "24000", features),
            )
        )
        assert not decision.emits_intent
        assert decision.rejections[0].reason is ReasonCode.DATA_GAP


def test_quote_instability_is_rejected() -> None:
    features = {**BULLISH_FEATURES, FEATURE_QUOTE_INSTABILITY: Decimal("0.90")}
    decision = CasMicrostructureStrategy().evaluate(
        _ctx(
            (_option(OptionType.CALL),),
            underlying=_underlying("24100", "24000", features),
        )
    )
    assert not decision.emits_intent
    assert decision.rejections[0].reason is ReasonCode.DATA_DEGRADED


def test_stale_snapshot_is_rejected() -> None:
    decision = CasMicrostructureStrategy().evaluate(
        _ctx((_option(OptionType.CALL),), now=CAS_NOW + timedelta(seconds=45))
    )
    assert not decision.emits_intent
    assert decision.rejections[0].reason is ReasonCode.DATA_STALE


def test_outside_closing_window_is_rejected() -> None:
    decision = CasMicrostructureStrategy().evaluate(
        _ctx((_option(OptionType.CALL),), now=CAS_NOW - timedelta(hours=2))
    )
    assert not decision.emits_intent
    assert decision.rejections[0].reason is ReasonCode.OUTSIDE_SESSION


def test_weekend_is_rejected() -> None:
    decision = CasMicrostructureStrategy().evaluate(
        _ctx((_option(OptionType.CALL),), now=CAS_NOW + timedelta(days=5))
    )
    assert not decision.emits_intent
    assert decision.rejections[0].reason is ReasonCode.OUTSIDE_SESSION


def test_expiring_contract_is_rejected() -> None:
    decision = CasMicrostructureStrategy().evaluate(
        _ctx((_option(OptionType.CALL, days_to_expiry=0),))
    )
    assert not decision.emits_intent
    assert decision.rejections[0].reason is ReasonCode.CONTRACT_EXPIRED


def test_thin_contract_is_rejected() -> None:
    decision = CasMicrostructureStrategy().evaluate(
        _ctx((_option(OptionType.CALL, open_interest=10),))
    )
    assert not decision.emits_intent
    assert decision.rejections[0].reason is ReasonCode.DEPTH_INSUFFICIENT


def test_frozen_entries_are_rejected() -> None:
    view = portfolio_view(entries_permitted=False)
    decision = CasMicrostructureStrategy().evaluate(
        _ctx((_option(OptionType.CALL),), view=view)
    )
    assert not decision.emits_intent
    assert decision.rejections[0].reason is ReasonCode.ENTRY_FROZEN


def test_wrong_candidate_count_is_rejected() -> None:
    decision = CasMicrostructureStrategy().evaluate(_ctx(()))
    assert not decision.emits_intent
    assert decision.rejections[0].reason is ReasonCode.INSTRUMENT_UNKNOWN


def test_microstructure_disagreement_emits_nothing() -> None:
    # Technically bullish, but the measured auction pressure is bearish.
    ctx = _ctx(
        (_option(OptionType.CALL),),
        underlying=_underlying("24100", "24000", BEARISH_FEATURES),
    )
    decision = CasMicrostructureStrategy().evaluate(ctx)
    assert not decision.emits_intent
    assert decision.rejections == ()


def test_option_type_mismatch_emits_nothing() -> None:
    ctx = _ctx((_option(OptionType.PUT),))
    decision = CasMicrostructureStrategy().evaluate(ctx)
    assert not decision.emits_intent
    assert decision.rejections == ()


def test_fresh_bearish_macro_overrides_bullish_technical() -> None:
    macro = _macro(MacroBias.BEARISH)
    ctx = _ctx(
        (_option(OptionType.PUT),),
        underlying=_underlying("24100", "24000", BEARISH_FEATURES),
        macro=macro,
    )
    assert _intent(ctx).setup_code == "CAS_PUT_BEARISH"


def test_stale_macro_falls_back_to_technical() -> None:
    macro = _macro(MacroBias.BEARISH, fresh=False)
    ctx = _ctx(
        (_option(OptionType.CALL),),
        underlying=_underlying("24100", "24000", BULLISH_FEATURES),
        macro=macro,
    )
    assert _intent(ctx).setup_code == "CAS_CALL_BULLISH"


def test_low_confidence_macro_falls_back_to_technical() -> None:
    macro = _macro(MacroBias.BEARISH, confidence="0.30")
    ctx = _ctx(
        (_option(OptionType.CALL),),
        underlying=_underlying("24100", "24000", BULLISH_FEATURES),
        macro=macro,
    )
    assert _intent(ctx).setup_code == "CAS_CALL_BULLISH"


def test_macro_never_bypasses_a_rejection() -> None:
    macro = _macro(MacroBias.BULLISH)
    ctx = _ctx(
        (_option(OptionType.CALL),),
        now=CAS_NOW + timedelta(seconds=45),
        macro=macro,
    )
    decision = CasMicrostructureStrategy().evaluate(ctx)
    assert not decision.emits_intent
    assert decision.rejections[0].reason is ReasonCode.DATA_STALE


def test_intent_id_is_deterministic() -> None:
    first = _intent(_ctx((_option(OptionType.CALL),)))
    second = _intent(_ctx((_option(OptionType.CALL),)))
    assert first.intent_id == second.intent_id
    assert first.correlation_id == second.correlation_id


def test_leg_carries_ratio_not_quantity() -> None:
    intent = _intent(_ctx((_option(OptionType.CALL),)))
    assert all(leg.ratio == 1 for leg in intent.legs)
    assert not hasattr(intent.legs[0], "quantity")


@pytest.mark.parametrize("instant", [CAS_NOW, CAS_NOW + timedelta(seconds=10)])
def test_inside_window_instants_are_accepted(instant: datetime) -> None:
    intent = _intent(_ctx((_option(OptionType.CALL),), now=instant))
    assert intent.setup_code == "CAS_CALL_BULLISH"
