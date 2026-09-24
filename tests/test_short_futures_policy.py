"""Stop-bounded commodity futures shorts, gated by risk policy.

A short future has no structural loss cap: only the protective stop bounds it.
That makes admitting one a policy decision, not a default. These tests pin both
directions of the switch, prove the long direction is unaffected, and prove the
carve-out did not open naked short options.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

import tests.factories as f
from trading.broker.paper import PaperBroker
from trading.config import load_config, load_risk_policy
from trading.config.risk_policy import LoadedRiskPolicy
from trading.domain.clock import FrozenClock
from trading.domain.contracts import (
    DerivativesContext,
    FeatureSnapshot,
    InstrumentSpec,
    IntentLeg,
    SizingRequest,
    TradeIntent,
)
from trading.domain.enums import (
    AssetClass,
    Exchange,
    InstrumentKind,
    OptionType,
    ReasonCode,
    Side,
)
from trading.domain.ids import SequentialIdFactory
from trading.domain.primitives import Money, Price, TickSize
from trading.risk import CapitalReservationService, RiskGateway, RiskGatewayRequest
from trading.risk.gateway import _short_is_admissible, _StructureKind
from trading.risk.limits import build_sizing_limits
from trading.risk.sizing.commodity_future import CommodityFutureSizingEngine
from trading.storage import TradingStore
from trading.strategies import CommodityFuturesStrategy, StrategyContext

ROOT = Path(__file__).resolve().parent.parent
RISK_POLICY = load_risk_policy(ROOT / "config" / "risk.yaml")
# The same policy with the switch off, to pin the fail-closed direction.
POLICY_WITHOUT_SHORTS = replace(
    RISK_POLICY,
    config=RISK_POLICY.config.model_copy(
        update={"allow_stop_bounded_futures_short": False}
    ),
)
ACCOUNT_CONFIG = load_config(ROOT / "config" / "base.yaml")
BROKER_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "broker"
NOW = f.NOW
COMMODITY_TICK = TickSize.of("1.00")
STRATEGY_ID = "commodity_futures_trend"


def future_instrument_spec(**overrides: object) -> InstrumentSpec:
    payload: dict[str, object] = {
        "trading_symbol": "MCX:CRUDEOIL26OCTFUT",
        "exchange": Exchange.MCX,
        "segment": "MCX_FO",
        "underlying": "CRUDEOIL",
        "instrument_kind": InstrumentKind.FUTURE,
        "provider_token": "tok-fut-1",
        "exchange_token": 101,
        "lot_size": 100,
        "tick_size": Decimal("1.00"),
        "price_precision": 0,
        "expiry": date(2026, 10, 17),
        "trading_session": "0900-2330",
        "source": "fixture",
        "verified_at": date(2026, 9, 1),
    }
    payload.update(overrides)
    return InstrumentSpec.model_validate(payload)


def future_snapshot(**overrides: object) -> FeatureSnapshot:
    payload: dict[str, object] = {
        "contract": f.future_contract(),
        "market": f.quote(
            bid=Price(Decimal("6850"), COMMODITY_TICK),
            ask=Price(Decimal("6851"), COMMODITY_TICK),
            bid_size=300,
            ask_size=300,
        ),
        "derivatives": DerivativesContext(
            days_to_expiry=30,
            underlying_price=Price(Decimal("6850"), COMMODITY_TICK),
        ),
    }
    payload.update(overrides)
    return f.snapshot(**payload)


def option_snapshot(**overrides: object) -> FeatureSnapshot:
    payload: dict[str, object] = {
        "contract": f.option_contract(),
        "market": f.quote(),
        "derivatives": DerivativesContext(
            days_to_expiry=10,
            open_interest=5000,
            option_type=OptionType.CALL,
            underlying_price=f.price("24000"),
        ),
    }
    payload.update(overrides)
    return f.snapshot(**payload)


def option_instrument_spec() -> InstrumentSpec:
    return InstrumentSpec(
        trading_symbol="NSE:NIFTY26SEP24000CE",
        exchange=Exchange.NFO,
        segment="NSE_FO",
        underlying="NIFTY",
        instrument_kind=InstrumentKind.OPTION,
        provider_token="tok-1",
        exchange_token=1,
        lot_size=75,
        tick_size=Decimal("0.05"),
        price_precision=2,
        expiry=date(2026, 9, 24),
        strike=Decimal("24000"),
        option_type=OptionType.CALL,
        trading_session="0915-1540",
        source="fixture",
        verified_at=date(2026, 9, 1),
    )


def commodity_intent(snapshot_id: str, side: Side) -> TradeIntent:
    """A single-leg futures intent carrying the real Layer 3 strategy id."""
    return f.intent(
        snapshot_id=snapshot_id,
        strategy_id=STRATEGY_ID,
        underlying="CRUDEOIL",
        asset_class=AssetClass.COMMODITY,
        setup_code=f"{side.value}_FUTURES_BULLISH",
        legs=(
            IntentLeg(
                leg_id="leg-1",
                contract=f.future_contract(),
                side=side,
                ratio=1,
            ),
        ),
        exit_template=f.exit_template(stop_distance_ticks=50),
        requested_risk=f.money("7000"),
        estimated_max_loss=f.money("10000"),
    )


def _gateway(
    policy: LoadedRiskPolicy,
    store: TradingStore,
    broker: PaperBroker,
    ids: SequentialIdFactory,
    clock: FrozenClock,
) -> RiskGateway:
    return RiskGateway(
        account_config=ACCOUNT_CONFIG,
        risk_policy=policy,
        reservation_service=CapitalReservationService(
            store, clock=clock, id_factory=ids
        ),
        margin_preview=broker,
        clock=clock,
        id_factory=ids,
        nifty_only_execution=False,
    )


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(NOW)


@pytest.fixture
def id_factory(clock: FrozenClock) -> SequentialIdFactory:
    return SequentialIdFactory(clock.instant)


@pytest.fixture
def store(clock: FrozenClock, tmp_path: Path) -> TradingStore:
    return TradingStore.open(tmp_path / "trading.db", clock=clock)


@pytest.fixture
def broker(clock: FrozenClock, id_factory: SequentialIdFactory) -> PaperBroker:
    return PaperBroker.from_fixtures(
        BROKER_FIXTURES,
        clock=clock,
        id_factory=id_factory,
    )


@pytest.fixture
def gateway(
    store: TradingStore,
    broker: PaperBroker,
    clock: FrozenClock,
    id_factory: SequentialIdFactory,
) -> RiskGateway:
    return _gateway(RISK_POLICY, store, broker, id_factory, clock)


@pytest.fixture
def gateway_without_shorts(
    store: TradingStore,
    broker: PaperBroker,
    clock: FrozenClock,
    id_factory: SequentialIdFactory,
) -> RiskGateway:
    return _gateway(POLICY_WITHOUT_SHORTS, store, broker, id_factory, clock)


def _future_request(side: Side) -> RiskGatewayRequest:
    snap = future_snapshot()
    return RiskGatewayRequest(
        intent=commodity_intent(snap.snapshot_id, side),
        feature_snapshot=snap,
        portfolio_snapshot=f.portfolio_snapshot(),
        instrument=future_instrument_spec(),
        event_risk_state=f.event_risk_state(scope="CRUDEOIL"),
    )


class TestShortFutureAdmissibility:
    def test_short_future_is_approved_under_policy(self, gateway: RiskGateway) -> None:
        """A stop-bounded short future is admitted, sized and reserved."""
        decision = gateway.evaluate(_future_request(Side.SELL))
        assert decision.permits_submission
        assert decision.recalculated_max_loss is not None
        assert decision.recalculated_max_loss > f.money("0")
        assert decision.capital_reservation_id is not None

    def test_long_future_is_still_approved(self, gateway: RiskGateway) -> None:
        """The short path must not have cost the long direction anything."""
        decision = gateway.evaluate(_future_request(Side.BUY))
        assert decision.permits_submission

    def test_short_future_is_refused_when_policy_disables_it(
        self, gateway_without_shorts: RiskGateway
    ) -> None:
        """Fail closed: with the switch off the short is refused by name."""
        decision = gateway_without_shorts.evaluate(_future_request(Side.SELL))
        assert not decision.permits_submission
        assert ReasonCode.RISK_LIMIT_TRADE in decision.reason_codes

    def test_long_future_is_unaffected_by_the_switch(
        self, gateway_without_shorts: RiskGateway
    ) -> None:
        decision = gateway_without_shorts.evaluate(_future_request(Side.BUY))
        assert decision.permits_submission


class TestCarveOutIsNarrow:
    def test_only_futures_shorts_are_admissible(self) -> None:
        """Every structure kind is explicit, so the carve-out cannot widen."""
        policy = RISK_POLICY.config
        assert _short_is_admissible(_StructureKind.DEBIT_SPREAD, policy)
        assert _short_is_admissible(_StructureKind.CREDIT_SPREAD, policy)
        assert _short_is_admissible(_StructureKind.IRON_CONDOR, policy)
        assert _short_is_admissible(_StructureKind.COMMODITY_FUTURE, policy)
        # A single-leg long option carries no short leg, so it is never admitted
        # through this path: naked short options stay disabled.
        assert not _short_is_admissible(_StructureKind.LONG_OPTION, policy)

    def test_naked_short_option_is_not_approved(self, gateway: RiskGateway) -> None:
        """The carve-out must not have opened short options."""
        snap = option_snapshot()
        request = RiskGatewayRequest(
            intent=f.intent(
                snapshot_id=snap.snapshot_id,
                strategy_id="positional_long_option",
                legs=(
                    IntentLeg(
                        leg_id="leg-1",
                        contract=f.option_contract(),
                        side=Side.SELL,
                        ratio=1,
                    ),
                ),
                exit_template=f.exit_template(stop_distance_ticks=50),
                requested_risk=f.money("6500"),
                estimated_max_loss=f.money("10000"),
            ),
            feature_snapshot=snap,
            portfolio_snapshot=f.portfolio_snapshot(),
            instrument=option_instrument_spec(),
            event_risk_state=f.event_risk_state(),
        )
        decision = gateway.evaluate(request)
        assert not decision.permits_submission


class TestUnallocatedStrategyFailsClosed:
    def test_unlisted_strategy_is_rejected_not_raised(
        self, gateway: RiskGateway
    ) -> None:
        """A missing allocation is a rejection, never an exception."""
        snap = future_snapshot()
        intent = f.intent(
            snapshot_id=snap.snapshot_id,
            strategy_id="a_strategy_with_no_allocation",
            underlying="CRUDEOIL",
            asset_class=AssetClass.COMMODITY,
            legs=(
                IntentLeg(
                    leg_id="leg-1",
                    contract=f.future_contract(),
                    side=Side.BUY,
                    ratio=1,
                ),
            ),
            exit_template=f.exit_template(stop_distance_ticks=50),
        )
        decision = gateway.evaluate(
            RiskGatewayRequest(
                intent=intent,
                feature_snapshot=snap,
                portfolio_snapshot=f.portfolio_snapshot(),
                instrument=future_instrument_spec(),
                event_risk_state=f.event_risk_state(scope="CRUDEOIL"),
            )
        )
        assert not decision.permits_submission
        assert ReasonCode.CAPITAL_UNAVAILABLE in decision.reason_codes


class TestShortSizingMechanics:
    def test_short_entry_prices_off_the_bid(self, broker: PaperBroker) -> None:
        """A sell trades at the bid; pricing off the ask would misstate the entry."""
        snap = future_snapshot()
        engine = CommodityFutureSizingEngine()
        portfolio = f.portfolio_snapshot(
            exposure=f.exposure(
                equity=f.money("5000000"),
                margin_available=f.money("5000000"),
            ),
        )
        limits = build_sizing_limits(
            portfolio,
            ACCOUNT_CONFIG.config.risk,
            RISK_POLICY.config,
            STRATEGY_ID,
            config_version=ACCOUNT_CONFIG.version,
        )
        request = SizingRequest(
            request_id="SIZE-SHORT-1",
            intent=commodity_intent(snap.snapshot_id, Side.SELL),
            feature_snapshot=snap,
            portfolio_snapshot=portfolio,
            limits=limits,
            requested_at=NOW,
        )
        short = engine.size(
            request,
            future_instrument_spec(),
            RISK_POLICY.config,
            broker,
            account_id="ACC-PAPER-1",
            account_risk=ACCOUNT_CONFIG.config.risk,
            preview_request_id="MARGIN-SHORT-1",
        )
        assert short.entry_price.value == Decimal("6850")
        assert short.approved_lots >= 1

        long_snap = future_snapshot()
        long_request = SizingRequest(
            request_id="SIZE-LONG-1",
            intent=commodity_intent(long_snap.snapshot_id, Side.BUY),
            feature_snapshot=long_snap,
            portfolio_snapshot=portfolio,
            limits=limits,
            requested_at=NOW,
        )
        long_result = engine.size(
            long_request,
            future_instrument_spec(),
            RISK_POLICY.config,
            broker,
            account_id="ACC-PAPER-1",
            account_risk=ACCOUNT_CONFIG.config.risk,
            preview_request_id="MARGIN-LONG-1",
        )
        assert long_result.entry_price.value == Decimal("6851")

    def test_short_and_long_share_the_stop_bounded_risk(
        self, broker: PaperBroker
    ) -> None:
        """Risk comes from stop distance, so direction must not change it."""
        snap = future_snapshot()
        engine = CommodityFutureSizingEngine()
        portfolio = f.portfolio_snapshot(
            exposure=f.exposure(
                equity=f.money("5000000"),
                margin_available=f.money("5000000"),
            ),
        )
        limits = build_sizing_limits(
            portfolio,
            ACCOUNT_CONFIG.config.risk,
            RISK_POLICY.config,
            STRATEGY_ID,
            config_version=ACCOUNT_CONFIG.version,
        )
        losses: list[Money] = []
        for side in (Side.BUY, Side.SELL):
            request = SizingRequest(
                request_id=f"SIZE-{side.value}",
                intent=commodity_intent(snap.snapshot_id, side),
                feature_snapshot=snap,
                portfolio_snapshot=portfolio,
                limits=limits,
                requested_at=NOW,
            )
            losses.append(
                engine.size(
                    request,
                    future_instrument_spec(),
                    RISK_POLICY.config,
                    broker,
                    account_id="ACC-PAPER-1",
                    account_risk=ACCOUNT_CONFIG.config.risk,
                    preview_request_id=f"MARGIN-{side.value}",
                ).recalculated_max_loss
            )
        assert losses[0] == losses[1]


class TestRealStrategyReadIsApproved:
    def test_bearish_commodity_read_reaches_an_approved_decision(
        self, gateway: RiskGateway
    ) -> None:
        """The capstone: Layer 3's own bearish read is admitted by Layer 2.

        Everything upstream of this test exercises a hand-built intent. This one
        runs the real ``CommodityFuturesStrategy`` and hands its untouched output
        to the real gateway, so the strategy, the structure dispatcher, the
        policy switch and the sizing engine are all proven to agree.
        """
        times = f.snapshot_times(
            event_time=NOW - timedelta(seconds=2),
            source_time=NOW - timedelta(seconds=1),
            receive_time=NOW - timedelta(milliseconds=500),
            calculation_time=NOW - timedelta(milliseconds=1),
        )
        derivatives = DerivativesContext(
            days_to_expiry=30,
            open_interest=9000,
            underlying_price=Price(Decimal("6700"), COMMODITY_TICK),
        )
        underlying = f.snapshot(
            snapshot_id="SNAP-COMMODITY-UNDER",
            contract=f.future_contract(),
            times=times,
            market=f.quote(
                bid=Price(Decimal("6699"), COMMODITY_TICK),
                ask=Price(Decimal("6700"), COMMODITY_TICK),
                last=Price(Decimal("6700"), COMMODITY_TICK),
                close=Price(Decimal("6800"), COMMODITY_TICK),
            ),
            derivatives=derivatives,
        )
        candidate = f.snapshot(
            snapshot_id="SNAP-COMMODITY-FUT",
            contract=f.future_contract(),
            times=times,
            market=f.quote(
                bid=Price(Decimal("6850"), COMMODITY_TICK),
                ask=Price(Decimal("6851"), COMMODITY_TICK),
                bid_size=300,
                ask_size=300,
            ),
            derivatives=derivatives,
        )
        # last < close, so the technical read is bearish without any macro input.
        strategy_ctx = StrategyContext(
            underlying=underlying,
            candidates=(candidate,),
            view=f.portfolio_view(),
            now=NOW,
            macro=None,
        )
        decision = CommodityFuturesStrategy().evaluate(strategy_ctx)
        assert decision.emits_intent
        intent = decision.intents[0]
        assert intent.legs[0].side is Side.SELL
        assert intent.strategy_id == STRATEGY_ID

        risk = gateway.evaluate(
            RiskGatewayRequest(
                intent=intent,
                feature_snapshot=underlying,
                portfolio_snapshot=f.portfolio_snapshot(),
                instrument=future_instrument_spec(),
                event_risk_state=f.event_risk_state(scope="CRUDEOIL"),
            )
        )
        assert risk.permits_submission, risk.reason_codes
        assert risk.capital_reservation_id is not None
