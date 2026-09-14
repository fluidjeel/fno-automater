"""Layer 3 -> Layer 2 seam: every intent Layer 3 emits must be classifiable.

Layer 3 and Layer 2 were built independently, and each layer's own suite is
blind to this boundary: Layer 3 tests assert the shape of an intent, while
Layer 2 tests construct intents by hand. A structure that Layer 3 really emits
can therefore reach the risk gateway and match no structure at all, which is
refused as ``INSTRUMENT_UNKNOWN`` — a misleading reason code that reads like a
bad symbol rather than a policy refusal.

These tests drive each strategy to a real ``TradeIntent`` and run it through
Layer 2's real dispatcher, so a disagreement between the two layers fails here
rather than in production.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tests.factories import (
    NOW,
    future_contract,
    index_contract,
    option_contract,
    portfolio_snapshot,
    portfolio_view,
    price,
    quote,
    snapshot,
    snapshot_times,
)
from trading.config import load_risk_policy
from trading.domain.contracts import (
    ContractRef,
    DerivativesContext,
    FeatureSnapshot,
    SnapshotTimes,
    TradeIntent,
)
from trading.domain.contracts.instrument import InstrumentSpec
from trading.domain.enums import (
    AssetClass,
    Exchange,
    InstrumentKind,
    OptionType,
    Side,
)
from trading.risk.gateway import (
    RiskGatewayRequest,
    _detect_structure,
    _short_is_admissible,
    _StructureKind,
)
from trading.risk.sizing import is_commodity_future
from trading.strategies import (
    CasMicrostructureStrategy,
    CommodityFuturesStrategy,
    DebitSpreadStrategy,
    LongOptionStrategy,
    MultiLegOptionsStrategy,
    Strategy,
    StrategyContext,
)
from trading.strategies.cas_microstructure import (
    FEATURE_AUCTION_IMBALANCE,
    FEATURE_MICROPRICE_EDGE_BPS,
    FEATURE_QUOTE_INSTABILITY,
    FEATURE_TRADE_FLOW_IMBALANCE,
)

NOW_CTX = NOW + timedelta(seconds=60)
# 2026-09-14 is a Monday; 09:45 UTC is 15:15 IST, inside the CAS window.
CAS_NOW = datetime(2026, 9, 14, 9, 45, tzinfo=UTC)

_OPTION_SPEC = InstrumentSpec(
    trading_symbol="NSE:NIFTY26SEP24000CE",
    exchange=Exchange.NFO,
    segment="NSE_FO",
    underlying="NIFTY",
    instrument_kind=InstrumentKind.OPTION,
    provider_token="1",
    exchange_token=1,
    lot_size=75,
    tick_size=Decimal("0.05"),
    price_precision=2,
    expiry=date(2026, 9, 24),
    strike=Decimal("24000"),
    option_type=OptionType.CALL,
    trading_session="0915-1540",
    source="fixture",
    verified_at=date(2026, 9, 11),
)

_FUTURE_SPEC = InstrumentSpec(
    trading_symbol="MCX:CRUDEOIL26OCTFUT",
    exchange=Exchange.MCX,
    segment="MCX_FO",
    underlying="CRUDEOIL",
    instrument_kind=InstrumentKind.FUTURE,
    provider_token="2",
    exchange_token=2,
    lot_size=100,
    tick_size=Decimal("1"),
    price_precision=1,
    expiry=date(2026, 10, 17),
    trading_session="0900-2330",
    source="fixture",
    verified_at=date(2026, 9, 11),
)


def _times(instant: datetime) -> SnapshotTimes:
    return snapshot_times(
        event_time=instant - timedelta(seconds=2),
        source_time=instant - timedelta(seconds=1),
        receive_time=instant - timedelta(milliseconds=500),
        calculation_time=instant - timedelta(milliseconds=200),
    )


def _underlying(
    last: str,
    close: str,
    *,
    contract: ContractRef | None = None,
    features: dict[str, Decimal] | None = None,
    derivatives: DerivativesContext | None = None,
    instant: datetime = NOW_CTX,
) -> FeatureSnapshot:
    return snapshot(
        snapshot_id="SNAP-UNDER",
        contract=contract if contract is not None else index_contract(),
        times=_times(instant),
        market=quote(last=price(last), close=price(close)),
        features={} if features is None else features,
        derivatives=derivatives,
    )


def _option(
    option_type: OptionType, strike: str, instant: datetime = NOW_CTX
) -> FeatureSnapshot:
    return snapshot(
        snapshot_id=f"SNAP-{option_type.value}-{strike}",
        contract=option_contract(
            option_type=option_type,
            strike=Decimal(strike),
            symbol=f"NIFTY26SEP{strike}{option_type.value}",
        ),
        times=_times(instant),
        market=quote(bid=price("118.00"), ask=price("118.10"), last=price("118.05")),
        derivatives=DerivativesContext(
            days_to_expiry=10,
            open_interest=5000,
            option_type=option_type,
            underlying_price=price("24000"),
        ),
    )


def _future(instant: datetime = NOW_CTX) -> FeatureSnapshot:
    return snapshot(
        snapshot_id="SNAP-FUT",
        contract=future_contract(),
        times=_times(instant),
        market=quote(bid=price("5800.00"), ask=price("5800.50"), last=price("5800.25")),
        derivatives=DerivativesContext(days_to_expiry=30, open_interest=9000),
    )


def _cas_features(sign: str) -> dict[str, Decimal]:
    return {
        FEATURE_AUCTION_IMBALANCE: Decimal(f"{sign}0.40"),
        FEATURE_TRADE_FLOW_IMBALANCE: Decimal(f"{sign}0.30"),
        FEATURE_MICROPRICE_EDGE_BPS: Decimal(f"{sign}5.00"),
        FEATURE_QUOTE_INSTABILITY: Decimal("0.10"),
    }


def _commodity_underlying(last: str, close: str) -> FeatureSnapshot:
    return _underlying(
        last,
        close,
        contract=future_contract(),
        derivatives=DerivativesContext(days_to_expiry=30, open_interest=9000),
    )


class _Case:
    """One strategy scenario and the Layer 2 structure it must resolve to."""

    __slots__ = ("ctx", "expected", "factory", "label", "spec")

    def __init__(
        self,
        label: str,
        factory: Callable[[], Strategy],
        ctx: StrategyContext,
        spec: InstrumentSpec,
        expected: str,
    ) -> None:
        self.label = label
        self.factory = factory
        self.ctx = ctx
        self.spec = spec
        self.expected = expected


def _cases() -> list[_Case]:
    bullish = _underlying("24100", "24000")
    bearish = _underlying("23900", "24000")
    return [
        _Case(
            "long_option_bullish",
            LongOptionStrategy,
            StrategyContext(
                bullish, (_option(OptionType.CALL, "24000"),), portfolio_view(), NOW_CTX
            ),
            _OPTION_SPEC,
            "LONG_OPTION",
        ),
        _Case(
            "long_option_bearish",
            LongOptionStrategy,
            StrategyContext(
                bearish, (_option(OptionType.PUT, "24000"),), portfolio_view(), NOW_CTX
            ),
            _OPTION_SPEC,
            "LONG_OPTION",
        ),
        _Case(
            "debit_spread_bullish",
            DebitSpreadStrategy,
            StrategyContext(
                bullish,
                (_option(OptionType.CALL, "24000"), _option(OptionType.CALL, "24100")),
                portfolio_view(),
                NOW_CTX,
            ),
            _OPTION_SPEC,
            "DEBIT_SPREAD",
        ),
        _Case(
            "debit_spread_bearish",
            DebitSpreadStrategy,
            StrategyContext(
                bearish,
                (_option(OptionType.PUT, "24000"), _option(OptionType.PUT, "23900")),
                portfolio_view(),
                NOW_CTX,
            ),
            _OPTION_SPEC,
            "DEBIT_SPREAD",
        ),
        _Case(
            "commodity_future_long",
            CommodityFuturesStrategy,
            StrategyContext(
                _commodity_underlying("5900", "5800"),
                (_future(),),
                portfolio_view(),
                NOW_CTX,
            ),
            _FUTURE_SPEC,
            "COMMODITY_FUTURE",
        ),
        _Case(
            "commodity_future_short",
            CommodityFuturesStrategy,
            StrategyContext(
                _commodity_underlying("5700", "5800"),
                (_future(),),
                portfolio_view(),
                NOW_CTX,
            ),
            _FUTURE_SPEC,
            "COMMODITY_FUTURE",
        ),
        _Case(
            "cas_bullish",
            CasMicrostructureStrategy,
            StrategyContext(
                _underlying(
                    "24100", "24000", features=_cas_features(""), instant=CAS_NOW
                ),
                (_option(OptionType.CALL, "24000", CAS_NOW),),
                portfolio_view(),
                CAS_NOW,
            ),
            _OPTION_SPEC,
            "LONG_OPTION",
        ),
        _Case(
            "cas_bearish",
            CasMicrostructureStrategy,
            StrategyContext(
                _underlying(
                    "23900", "24000", features=_cas_features("-"), instant=CAS_NOW
                ),
                (_option(OptionType.PUT, "24000", CAS_NOW),),
                portfolio_view(),
                CAS_NOW,
            ),
            _OPTION_SPEC,
            "LONG_OPTION",
        ),
        _Case(
            "credit_spread_bull_put",
            MultiLegOptionsStrategy,
            StrategyContext(
                bullish,
                (_option(OptionType.PUT, "23900"), _option(OptionType.PUT, "24000")),
                portfolio_view(),
                NOW_CTX,
            ),
            _OPTION_SPEC,
            "CREDIT_SPREAD",
        ),
        _Case(
            "credit_spread_bear_call",
            MultiLegOptionsStrategy,
            StrategyContext(
                bearish,
                (_option(OptionType.CALL, "24000"), _option(OptionType.CALL, "24100")),
                portfolio_view(),
                NOW_CTX,
            ),
            _OPTION_SPEC,
            "CREDIT_SPREAD",
        ),
    ]


CASES = _cases()


def _request(case: _Case) -> tuple[RiskGatewayRequest, TradeIntent]:
    decision = case.factory().evaluate(case.ctx)
    assert decision.emits_intent, f"{case.label} emitted no intent"
    intent = decision.intents[0]
    return (
        RiskGatewayRequest(
            intent=intent,
            feature_snapshot=case.ctx.underlying,
            portfolio_snapshot=portfolio_snapshot(),
            instrument=case.spec,
        ),
        intent,
    )


@pytest.mark.parametrize("case", CASES, ids=[case.label for case in CASES])
def test_every_layer3_intent_is_classifiable_by_layer2(case: _Case) -> None:
    """The seam invariant: Layer 2 never sees a structure it cannot name."""
    request, intent = _request(case)
    structure = _detect_structure(request)
    assert structure is not None, (
        f"{case.label} emitted {intent.setup_code}, which Layer 2 cannot classify; "
        "it would be refused as INSTRUMENT_UNKNOWN rather than on its real merits"
    )
    assert structure.name == case.expected, (
        f"{case.label}: Layer 2 resolved {structure.name}, expected {case.expected}"
    )


@pytest.mark.parametrize("case", CASES, ids=[case.label for case in CASES])
def test_no_short_leg_is_ever_unclassifiable(case: _Case) -> None:
    """A short leg is a policy question, never an unknown-instrument question."""
    request, intent = _request(case)
    if not any(leg.side is Side.SELL for leg in intent.legs):
        pytest.skip(f"{case.label}: no short leg")
    assert _detect_structure(request) is not None


def test_short_futures_are_classified_not_treated_as_unknown_instruments() -> None:
    """Regression: a bearish commodity read produced an unclassifiable intent.

    A short future is a commodity future, so Layer 2 must classify it. It was
    matching no structure at all and being refused as INSTRUMENT_UNKNOWN, which
    reads like a bad symbol and hid the real question — whether the risk layer
    admits a stop-bounded short. That question is now answered by policy, and
    the classification is what makes the answer reachable.
    """
    case = next(c for c in CASES if c.label == "commodity_future_short")
    request, intent = _request(case)
    assert intent.legs[0].side is Side.SELL

    structure = _detect_structure(request)
    assert structure is not None, "a short future must not look like a bad symbol"
    assert structure.name == "COMMODITY_FUTURE"


def test_short_future_admissibility_follows_the_policy_switch() -> None:
    """The seam resolves the structure; policy alone decides the short."""
    root = Path(__file__).resolve().parent.parent
    policy = load_risk_policy(root / "config" / "risk.yaml")
    short = _StructureKind.COMMODITY_FUTURE
    assert _short_is_admissible(short, policy.config) is (
        policy.config.allow_stop_bounded_futures_short
    )
    disabled = policy.config.model_copy(
        update={"allow_stop_bounded_futures_short": False}
    )
    assert not _short_is_admissible(short, disabled)


def test_commodity_futures_are_classified_in_both_directions() -> None:
    """The structure predicate is about structure, not about admissibility."""
    for label in ("commodity_future_long", "commodity_future_short"):
        case = next(c for c in CASES if c.label == label)
        _, intent = _request(case)
        assert is_commodity_future(intent, case.spec), f"{label} not detected"


def test_equity_index_intents_are_not_mistaken_for_commodities() -> None:
    """Guard: only a commodity asset class may take the commodity path."""
    case = next(c for c in CASES if c.label == "long_option_bullish")
    _, intent = _request(case)
    assert intent.asset_class is AssetClass.EQUITY_INDEX
    assert not is_commodity_future(intent, _OPTION_SPEC)
