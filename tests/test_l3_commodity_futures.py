"""Layer 3 slice 3: directional commodity futures."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from tests.factories import (
    NOW,
    portfolio_view,
    price,
    quote,
    snapshot,
)
from trading.domain.contracts import (
    ContractRef,
    DerivativesContext,
    FeatureSnapshot,
)
from trading.domain.enums import (
    AssetClass,
    Exchange,
    InstrumentKind,
    ReasonCode,
    Side,
)
from trading.strategies import (
    CommodityFuturesStrategy,
    MacroAssessment,
    MacroBias,
    StrategyContext,
)

NOW_CTX = NOW + timedelta(seconds=60)


def _commodity_contract() -> ContractRef:
    return ContractRef.model_validate(
        {
            "exchange": Exchange.MCX,
            "symbol": "CRUDEOIL",
            "instrument_kind": InstrumentKind.INDEX,
            "asset_class": AssetClass.COMMODITY,
            "underlying": "CRUDEOIL",
        }
    )


def _future_contract() -> ContractRef:
    return ContractRef.model_validate(
        {
            "exchange": Exchange.MCX,
            "symbol": "CRUDEOIL26SEPFUT",
            "instrument_kind": InstrumentKind.FUTURE,
            "asset_class": AssetClass.COMMODITY,
            "underlying": "CRUDEOIL",
            "expiry": date(2026, 9, 24),
        }
    )


def _underlying(last: str, close: str) -> FeatureSnapshot:
    return snapshot(
        snapshot_id="SNAP-COMM",
        contract=_commodity_contract(),
        market=quote(last=price(last), close=price(close)),
    )


def _future() -> FeatureSnapshot:
    return snapshot(
        snapshot_id="SNAP-FUT",
        contract=_future_contract(),
        market=quote(bid=price("7100.00"), ask=price("7101.00"), last=price("7100.50")),
        derivatives=DerivativesContext(
            days_to_expiry=10,
            open_interest=800,
            underlying_price=price("7100"),
        ),
    )


def _ctx(underlying: FeatureSnapshot, **overrides: Any) -> StrategyContext:
    return StrategyContext(
        underlying=underlying,
        candidates=(_future(),),
        view=overrides.get("view") or portfolio_view(),
        now=overrides.get("now", NOW_CTX),
        macro=overrides.get("macro"),
    )


def test_bullish_emits_long_futures() -> None:
    decision = CommodityFuturesStrategy().evaluate(_ctx(_underlying("7150", "7100")))
    assert decision.emits_intent
    assert decision.intents[0].legs[0].side is Side.BUY
    assert decision.intents[0].setup_code == "BUY_FUTURES_BULLISH"


def test_bearish_emits_short_futures() -> None:
    decision = CommodityFuturesStrategy().evaluate(_ctx(_underlying("7050", "7100")))
    assert decision.emits_intent
    assert decision.intents[0].legs[0].side is Side.SELL
    assert decision.intents[0].setup_code == "SELL_FUTURES_BEARISH"


def test_neutral_emits_nothing() -> None:
    decision = CommodityFuturesStrategy().evaluate(_ctx(_underlying("7100", "7100")))
    assert not decision.emits_intent
    assert decision.rejections == ()


def test_stop_bounded_max_loss_equals_requested_risk() -> None:
    intent = (
        CommodityFuturesStrategy()
        .evaluate(_ctx(_underlying("7150", "7100")))
        .intents[0]
    )
    assert intent.requested_risk == intent.estimated_max_loss
    assert intent.exit_template.stop_distance_ticks > 0


def test_short_expiry_future_is_ineligible() -> None:
    future = _future().model_copy(
        update={
            "derivatives": DerivativesContext(
                days_to_expiry=1, underlying_price=price("7100")
            )
        }
    )
    ctx = StrategyContext(
        underlying=_underlying("7150", "7100"),
        candidates=(future,),
        view=portfolio_view(),
        now=NOW_CTX,
        macro=None,
    )
    decision = CommodityFuturesStrategy().evaluate(ctx)
    assert not decision.emits_intent
    assert decision.rejections[0].reason is ReasonCode.CONTRACT_EXPIRED


def test_deterministic_intent_id() -> None:
    first = CommodityFuturesStrategy().evaluate(_ctx(_underlying("7150", "7100")))
    second = CommodityFuturesStrategy().evaluate(_ctx(_underlying("7150", "7100")))
    assert first.intents[0].intent_id == second.intents[0].intent_id


def test_bearish_macro_overrides_technical() -> None:
    macro = MacroAssessment(
        regime="RISK_OFF",
        directional_bias=MacroBias.BEARISH,
        confidence=Decimal("0.8"),
        fresh_until=NOW_CTX + timedelta(hours=1),
        evidence_ids=("src-1",),
        model_version="macro-agent-v1",
    )
    # Technical is bullish, but the fresh bearish macro wins.
    decision = CommodityFuturesStrategy().evaluate(
        _ctx(_underlying("7150", "7100"), macro=macro)
    )
    assert decision.intents[0].legs[0].side is Side.SELL
