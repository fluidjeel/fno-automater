"""Commodity futures sizing engine (L2-012).

Invariant 4: Layer 2 recalculates stop-distance max loss at decision time.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

import tests.factories as f
from trading.broker.paper import PaperBroker
from trading.config import load_config, load_risk_policy
from trading.domain.clock import FrozenClock
from trading.domain.contracts import (
    DerivativesContext,
    FeatureSnapshot,
    InstrumentSpec,
    IntentLeg,
    SizingRequest,
)
from trading.domain.enums import AssetClass, Exchange, InstrumentKind, Side
from trading.domain.ids import SequentialIdFactory
from trading.domain.primitives import Price, TickSize
from trading.risk.limits import build_sizing_limits
from trading.risk.sizing.commodity_future import (
    CommodityFutureSizingEngine,
    is_commodity_future,
)

ROOT = Path(__file__).resolve().parent.parent
RISK_POLICY = load_risk_policy(ROOT / "config" / "risk.yaml")
ACCOUNT_CONFIG = load_config(ROOT / "config" / "base.yaml")
BROKER_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "broker"
NOW = f.NOW
COMMODITY_TICK = TickSize.of("1.00")


def instrument_spec(**overrides: object) -> InstrumentSpec:
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


def commodity_intent(snapshot_id: str) -> object:
    return f.intent(
        snapshot_id=snapshot_id,
        underlying="CRUDEOIL",
        asset_class=AssetClass.COMMODITY,
        setup_code="COMMODITY_TREND",
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


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(NOW)


@pytest.fixture
def broker(clock: FrozenClock) -> PaperBroker:
    return PaperBroker.from_fixtures(
        BROKER_FIXTURES,
        clock=clock,
        id_factory=SequentialIdFactory(clock.instant),
    )


@pytest.fixture
def engine() -> CommodityFutureSizingEngine:
    return CommodityFutureSizingEngine()


def _sizing_request(**overrides: object) -> SizingRequest:
    snap = future_snapshot()
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
        "positional_index_options_poc",
        config_version=ACCOUNT_CONFIG.version,
    )
    limits = limits.model_copy(update={"max_loss_per_trade": f.money("50000")})
    return f.sizing_request(
        intent=commodity_intent(snap.snapshot_id),
        feature_snapshot=snap,
        portfolio_snapshot=portfolio,
        limits=limits,
        **overrides,
    )


class TestCommodityFutureDetection:
    def test_recognizes_long_commodity_future(self) -> None:
        request = _sizing_request()
        assert is_commodity_future(request.intent, instrument_spec())


class TestCommodityFutureSizing:
    def test_sizes_from_stop_distance_and_margin_preview(
        self,
        engine: CommodityFutureSizingEngine,
        broker: PaperBroker,
    ) -> None:
        """Max loss derives from stop distance times lot size, not strategy estimate."""
        request = _sizing_request()
        result = engine.size(
            request,
            instrument_spec(),
            RISK_POLICY.config,
            broker,
            account_id="ACC-PAPER-1",
            account_risk=ACCOUNT_CONFIG.config.risk,
            preview_request_id="MARGIN-FUT-1",
        )
        assert result.approved_lots >= 1
        assert result.recalculated_max_loss > f.money("0")
        assert result.recalculated_max_loss != request.intent.estimated_max_loss
        assert result.stop_distance_ticks == 50
