"""DISC-A32: DISCOVERY margin-aware sizing, per-trade lot cap, cycle margin."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

import tests.factories as f
from tests.test_disc_a2_discovery_sizing import (
    ACCOUNT_CONFIG,
    BROKER_FIXTURES,
    DISCOVERY_CFG,
    RISK_POLICY,
    _discovery_gateway,
    _straddle_intent,
    _straddle_leg_snapshots,
)
from tests.test_risk_gateway import instrument_spec
from trading.broker.paper import PaperBroker
from trading.broker.ports import MarginPreviewRequest, MarginPreviewResult
from trading.domain.clock import FrozenClock
from trading.domain.contracts import DerivativesContext, FeatureSnapshot, IntentLeg
from trading.domain.contracts.intent import TradeIntent
from trading.domain.enums import (
    FamilyId,
    ModeId,
    OptionType,
    ReasonCode,
    RiskAction,
    Side,
)
from trading.domain.ids import SequentialIdFactory
from trading.domain.primitives import Currency, Money, Percent
from trading.risk import CapitalReservationService, RiskGateway, RiskGatewayRequest
from trading.risk.discovery_sizing import (
    apply_discovery_lots,
    apply_discovery_margin_sizing,
    apply_discovery_trade_lot_cap,
    discovery_effective_cost_per_lot,
)
from trading.risk.mode_ledger import FourModeBook
from trading.storage.trading_store import TradingStore

ROOT = Path(__file__).resolve().parent.parent
NOW = f.NOW
EXPIRY = date(2026, 10, 1)


def _money(val: str) -> Money:
    return Money.of(val, Currency.INR)


class FixedMarginPreview:
    """Broker margin preview returning a fixed per-structure margin."""

    def __init__(self, margin_per_lot: str) -> None:
        self._margin = _money(margin_per_lot)

    def preview_margin(self, request: MarginPreviewRequest) -> MarginPreviewResult:
        return MarginPreviewResult(
            request_id=request.request_id,
            as_of=NOW,
            margin_required=self._margin,
            margin_available_after=_money("0"),
            confirmed=True,
        )


def _option(
    symbol: str,
    *,
    strike: str,
    option_type: OptionType,
    bid: str,
    ask: str,
) -> FeatureSnapshot:
    return f.snapshot(
        contract=f.option_contract(
            symbol=symbol,
            strike=Decimal(strike),
            option_type=option_type,
            expiry=EXPIRY,
        ),
        market=f.quote(
            bid=f.price(bid),
            ask=f.price(ask),
            last=f.price(ask),
            bid_size=500,
            ask_size=500,
        ),
        derivatives=DerivativesContext(
            days_to_expiry=10,
            open_interest=5000,
            option_type=option_type,
            underlying_price=f.price("24500"),
        ),
    )


def _iron_butterfly_candidates() -> tuple[FeatureSnapshot, ...]:
    return (
        _option(
            "NIFTY26OCT22950PE",
            strike="22950",
            option_type=OptionType.PUT,
            bid="8.00",
            ask="8.05",
        ),
        _option(
            "NIFTY26OCT23000PE",
            strike="23000",
            option_type=OptionType.PUT,
            bid="19.95",
            ask="20.05",
        ),
        _option(
            "NIFTY26OCT23000CE",
            strike="23000",
            option_type=OptionType.CALL,
            bid="19.95",
            ask="20.05",
        ),
        _option(
            "NIFTY26OCT23050CE",
            strike="23050",
            option_type=OptionType.CALL,
            bid="8.00",
            ask="8.05",
        ),
    )


def _call_butterfly_candidates() -> tuple[FeatureSnapshot, ...]:
    return (
        _option(
            "NIFTY26OCT24460CE",
            strike="24460",
            option_type=OptionType.CALL,
            bid="29.00",
            ask="29.05",
        ),
        _option(
            "NIFTY26OCT24500CE",
            strike="24500",
            option_type=OptionType.CALL,
            bid="9.00",
            ask="9.05",
        ),
        _option(
            "NIFTY26OCT24540CE",
            strike="24540",
            option_type=OptionType.CALL,
            bid="4.00",
            ask="4.05",
        ),
    )


def _iron_butterfly_intent(candidates: tuple[FeatureSnapshot, ...]) -> TradeIntent:
    by_symbol = {item.contract.symbol: item for item in candidates}
    return f.intent(
        mode_id=ModeId.M4_STRATEGIC_POSITIONAL,
        family_id=FamilyId.short_iron_butterfly_defined.value,
        snapshot_id=candidates[0].snapshot_id,
        requested_risk=_money("50000"),
        estimated_max_loss=_money("50000"),
        legs=(
            IntentLeg(
                leg_id="leg-long-put",
                contract=by_symbol["NIFTY26OCT22950PE"].contract,
                side=Side.BUY,
                ratio=1,
            ),
            IntentLeg(
                leg_id="leg-long-call",
                contract=by_symbol["NIFTY26OCT23050CE"].contract,
                side=Side.BUY,
                ratio=1,
            ),
            IntentLeg(
                leg_id="leg-short-put",
                contract=by_symbol["NIFTY26OCT23000PE"].contract,
                side=Side.SELL,
                ratio=1,
            ),
            IntentLeg(
                leg_id="leg-short-call",
                contract=by_symbol["NIFTY26OCT23000CE"].contract,
                side=Side.SELL,
                ratio=1,
            ),
        ),
    )


def _call_butterfly_intent(candidates: tuple[FeatureSnapshot, ...]) -> TradeIntent:
    by_symbol = {item.contract.symbol: item for item in candidates}
    return f.intent(
        mode_id=ModeId.M4_STRATEGIC_POSITIONAL,
        family_id=FamilyId.long_call_butterfly.value,
        snapshot_id=candidates[0].snapshot_id,
        entry_policy=f.entry_policy(max_spread=Percent.from_percent("2")),
        requested_risk=_money("50000"),
        estimated_max_loss=_money("50000"),
        legs=(
            IntentLeg(
                leg_id="leg-low-wing",
                contract=by_symbol["NIFTY26OCT24460CE"].contract,
                side=Side.BUY,
                ratio=1,
            ),
            IntentLeg(
                leg_id="leg-high-wing",
                contract=by_symbol["NIFTY26OCT24540CE"].contract,
                side=Side.BUY,
                ratio=1,
            ),
            IntentLeg(
                leg_id="leg-short-body",
                contract=by_symbol["NIFTY26OCT24500CE"].contract,
                side=Side.SELL,
                ratio=2,
            ),
        ),
    )


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(NOW + timedelta(seconds=60))


@pytest.fixture
def id_factory(clock: FrozenClock) -> SequentialIdFactory:
    return SequentialIdFactory(clock.instant)


@pytest.fixture
def store(clock: FrozenClock, tmp_path: Path) -> TradingStore:
    return TradingStore.open(tmp_path / "a32.sqlite", clock=clock)


@pytest.fixture
def broker(clock: FrozenClock, id_factory: SequentialIdFactory) -> PaperBroker:
    return PaperBroker.from_fixtures(
        BROKER_FIXTURES,
        clock=clock,
        id_factory=id_factory,
    )


def _gateway_with_margin_preview(
    store: TradingStore,
    clock: FrozenClock,
    id_factory: SequentialIdFactory,
    margin_preview: FixedMarginPreview,
) -> RiskGateway:
    book = FourModeBook(discovery_config=DISCOVERY_CFG)
    return RiskGateway(
        account_config=ACCOUNT_CONFIG,
        risk_policy=RISK_POLICY,
        reservation_service=CapitalReservationService(
            store, clock=clock, id_factory=id_factory
        ),
        margin_preview=margin_preview,
        clock=clock,
        id_factory=id_factory,
        mode_book=book,
    )


class TestDiscA32EffectiveCost:
    def test_effective_cost_uses_broker_margin_when_larger(self) -> None:
        cost = _money("3000")
        margin = _money("200000")
        assert discovery_effective_cost_per_lot(cost, margin) == margin

    def test_guide_lots_use_effective_cost(self) -> None:
        guide = _money("21000")
        result = apply_discovery_lots(
            cost_per_lot=_money("3000"),
            guide=guide,
            margin_per_lot=_money("200000"),
        )
        assert result.approved_lots == 1
        assert result.one_lot_over_guide is True


class TestDiscA32DiscoveryGateway:
    def test_iron_butterfly_capped_at_max_lots_per_trade(
        self,
        store: TradingStore,
        clock: FrozenClock,
        id_factory: SequentialIdFactory,
    ) -> None:
        """Iron butterfly with low max-loss but high broker margin → ≤2 lots."""
        gateway = _gateway_with_margin_preview(
            store,
            clock,
            id_factory,
            FixedMarginPreview("5000"),
        )
        candidates = _iron_butterfly_candidates()
        intent = _iron_butterfly_intent(candidates)
        leg_snapshots = {
            leg.leg_id: next(
                snap
                for snap in candidates
                if snap.contract.symbol == leg.contract.symbol
            )
            for leg in intent.legs
        }
        decision = gateway.evaluate(
            RiskGatewayRequest(
                intent=intent,
                feature_snapshot=candidates[0],
                portfolio_snapshot=f.portfolio_snapshot(
                    exposure=f.exposure(
                        equity=_money("700000"),
                        margin_available=_money("700000"),
                    )
                ),
                instrument=instrument_spec(),
                leg_snapshots=leg_snapshots,
                event_risk_state=f.event_risk_state(),
            )
        )
        assert decision.action is RiskAction.RESIZE
        assert decision.approved_legs
        assert decision.approved_legs[0].lots.count == DISCOVERY_CFG.max_lots_per_trade
        assert "max_lots_per_trade" in decision.applied_limits
        assert "RESIZED" in decision.applied_limits

    def test_near_margin_limit_gets_one_lot_not_eight(
        self,
        store: TradingStore,
        clock: FrozenClock,
        id_factory: SequentialIdFactory,
    ) -> None:
        """Portfolio near margin limit → 1 lot, not 8."""
        gateway = _gateway_with_margin_preview(
            store,
            clock,
            id_factory,
            FixedMarginPreview("5000"),
        )
        candidates = _iron_butterfly_candidates()
        intent = _iron_butterfly_intent(candidates)
        leg_snapshots = {
            leg.leg_id: next(
                snap
                for snap in candidates
                if snap.contract.symbol == leg.contract.symbol
            )
            for leg in intent.legs
        }
        decision = gateway.evaluate(
            RiskGatewayRequest(
                intent=intent,
                feature_snapshot=candidates[0],
                portfolio_snapshot=f.portfolio_snapshot(
                    exposure=f.exposure(
                        equity=_money("700000"),
                        margin_available=_money("8000"),
                    )
                ),
                instrument=instrument_spec(),
                leg_snapshots=leg_snapshots,
                event_risk_state=f.event_risk_state(),
            )
        )
        assert decision.action is RiskAction.RESIZE
        assert decision.approved_legs
        assert decision.approved_legs[0].lots.count == 1
        assert "broker_margin_available" in decision.applied_limits
        assert "RESIZED" in decision.applied_limits

    def test_cycle_margin_reserved_downsizes_later_intent(
        self,
        store: TradingStore,
        broker: PaperBroker,
        clock: FrozenClock,
        id_factory: SequentialIdFactory,
    ) -> None:
        """Earlier cycle margin consumption leaves 1 lot for the next intent."""
        gateway = _discovery_gateway(store, broker, clock, id_factory)
        legs = _straddle_leg_snapshots()
        intent = _straddle_intent(snapshot_id=legs["call"].snapshot_id)
        portfolio = f.portfolio_snapshot(
            exposure=f.exposure(
                equity=_money("700000"),
                margin_available=_money("700000"),
            )
        )
        first = gateway.evaluate(
            RiskGatewayRequest(
                intent=intent,
                feature_snapshot=legs["call"],
                portfolio_snapshot=portfolio,
                instrument=instrument_spec(),
                leg_snapshots=legs,
                event_risk_state=f.event_risk_state(),
            )
        )
        assert first.action in {RiskAction.APPROVE, RiskAction.RESIZE}
        assert first.margin_required is not None
        second = gateway.evaluate(
            RiskGatewayRequest(
                intent=f.intent(
                    intent_id="INT-B",
                    snapshot_id=legs["call"].snapshot_id,
                    mode_id=ModeId.M4_STRATEGIC_POSITIONAL,
                    family_id=FamilyId.long_straddle.value,
                    requested_risk=_money("25000"),
                    estimated_max_loss=_money("30000"),
                    legs=(
                        IntentLeg(
                            leg_id="call",
                            contract=legs["call"].contract,
                            side=Side.BUY,
                            ratio=1,
                        ),
                        IntentLeg(
                            leg_id="put",
                            contract=legs["put"].contract,
                            side=Side.BUY,
                            ratio=1,
                        ),
                    ),
                ),
                feature_snapshot=legs["call"],
                portfolio_snapshot=portfolio,
                instrument=instrument_spec(),
                leg_snapshots=legs,
                event_risk_state=f.event_risk_state(),
                cycle_margin_reserved=first.margin_required,
            )
        )
        assert second.action in {RiskAction.APPROVE, RiskAction.RESIZE}
        assert second.approved_legs
        if first.approved_legs and first.approved_legs[0].lots.count >= 1:
            assert (
                second.approved_legs[0].lots.count <= first.approved_legs[0].lots.count
            )

    def test_call_butterfly_body_ratio_preserved_after_resize(
        self,
        store: TradingStore,
        clock: FrozenClock,
        id_factory: SequentialIdFactory,
    ) -> None:
        """1:2:1 butterfly keeps body leg in approved legs after margin cap."""
        gateway = _gateway_with_margin_preview(
            store,
            clock,
            id_factory,
            FixedMarginPreview("5000"),
        )
        candidates = _call_butterfly_candidates()
        intent = _call_butterfly_intent(candidates)
        leg_snapshots = {
            leg.leg_id: next(
                snap
                for snap in candidates
                if snap.contract.symbol == leg.contract.symbol
            )
            for leg in intent.legs
        }
        decision = gateway.evaluate(
            RiskGatewayRequest(
                intent=intent,
                feature_snapshot=candidates[0],
                portfolio_snapshot=f.portfolio_snapshot(
                    exposure=f.exposure(
                        equity=_money("700000"),
                        margin_available=_money("700000"),
                    )
                ),
                instrument=instrument_spec(),
                leg_snapshots=leg_snapshots,
                event_risk_state=f.event_risk_state(),
            )
        )
        assert decision.action in {RiskAction.APPROVE, RiskAction.RESIZE}
        assert decision.approved_legs
        assert decision.approved_legs[0].lots.count <= DISCOVERY_CFG.max_lots_per_trade
        body = next(leg for leg in intent.legs if leg.leg_id == "leg-short-body")
        approved_body = next(
            leg for leg in decision.approved_legs if leg.leg_id == body.leg_id
        )
        assert approved_body.lots.count == decision.approved_legs[0].lots.count


class TestDiscA32SizingPrimitives:
    def test_trade_lot_cap_never_rejects(self) -> None:
        result = apply_discovery_trade_lot_cap(
            approved_lots=8,
            recalculated_max_loss=_money("80000"),
            cost_per_lot=_money("10000"),
            max_lots_per_trade=2,
        )
        assert result.approved_lots == 2
        assert "max_lots_per_trade" in result.applied_limits

    def test_margin_sizing_always_caps_to_affordable(self) -> None:
        result = apply_discovery_margin_sizing(
            approved_lots=8,
            recalculated_max_loss=_money("24000"),
            estimated_margin=_money("1600000"),
            cost_per_lot=_money("3000"),
            margin_per_lot=_money("200000"),
            margin_lots=3,
            broker_margin_available=_money("500000"),
        )
        assert result.approved_lots == 2
        assert ReasonCode.MARGIN_INSUFFICIENT in result.strict_would_block


class TestDiscA32StrictUnchanged:
    def test_strict_still_rejects_margin_insufficient(
        self,
        store: TradingStore,
        broker: PaperBroker,
        clock: FrozenClock,
        id_factory: SequentialIdFactory,
    ) -> None:
        gateway = RiskGateway(
            account_config=ACCOUNT_CONFIG,
            risk_policy=RISK_POLICY,
            reservation_service=CapitalReservationService(
                store, clock=clock, id_factory=id_factory
            ),
            margin_preview=broker,
            clock=clock,
            id_factory=id_factory,
            mode_book=FourModeBook(),
        )
        legs = _straddle_leg_snapshots()
        intent = _straddle_intent(snapshot_id=legs["call"].snapshot_id)
        decision = gateway.evaluate(
            RiskGatewayRequest(
                intent=intent,
                feature_snapshot=legs["call"],
                portfolio_snapshot=f.portfolio_snapshot(
                    exposure=f.exposure(margin_available=_money("100"))
                ),
                instrument=instrument_spec(),
                leg_snapshots=legs,
                event_risk_state=f.event_risk_state(),
            )
        )
        assert decision.action is RiskAction.REJECT
        assert ReasonCode.MIN_LOT_EXCEEDS_BUDGET in decision.reason_codes
