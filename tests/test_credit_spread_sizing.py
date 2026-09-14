"""Credit spread sizing engine (L2-013).

Invariant 4: Layer 2 recalculates defined max loss from net credit at decision time.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

import tests.factories as f
from trading.broker.paper import PaperBroker
from trading.config import load_config, load_risk_policy
from trading.domain.clock import FrozenClock
from trading.domain.contracts import (
    ContractRef,
    DerivativesContext,
    FeatureSnapshot,
    Greeks,
    IntentLeg,
    SizingRequest,
    TradeIntent,
)
from trading.domain.enums import OptionType, Side
from trading.domain.ids import SequentialIdFactory
from trading.domain.primitives import LotSize
from trading.risk.limits import build_sizing_limits
from trading.risk.sizing.credit_spread import CreditSpreadSizingEngine, is_credit_spread

ROOT = Path(__file__).resolve().parent.parent
RISK_POLICY = load_risk_policy(ROOT / "config" / "risk.yaml")
ACCOUNT_CONFIG = load_config(ROOT / "config" / "base.yaml")
BROKER_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "broker"
NOW = f.NOW
LOT_SIZE = LotSize(75)


def short_contract() -> ContractRef:
    return f.option_contract(symbol="NIFTY26SEP24000CE", strike=Decimal("24000"))


def long_contract() -> ContractRef:
    return f.option_contract(symbol="NIFTY26SEP24200CE", strike=Decimal("24200"))


def short_snapshot(**overrides: object) -> FeatureSnapshot:
    payload: dict[str, object] = {
        "contract": short_contract(),
        "market": f.quote(
            bid=f.price("49.95"),
            ask=f.price("50.00"),
            bid_size=300,
            ask_size=300,
        ),
        "derivatives": DerivativesContext(
            days_to_expiry=10,
            open_interest=5000,
            option_type=OptionType.CALL,
            underlying_price=f.price("24000"),
            greeks=Greeks(
                model="bs",
                calculation_version="1",
                converged=True,
                delta=Decimal("0.45"),
            ),
        ),
    }
    payload.update(overrides)
    return f.snapshot(**payload)


def long_snapshot(**overrides: object) -> FeatureSnapshot:
    payload: dict[str, object] = {
        "contract": long_contract(),
        "market": f.quote(
            bid=f.price("24.95"),
            ask=f.price("25.00"),
            bid_size=300,
            ask_size=300,
        ),
        "derivatives": DerivativesContext(
            days_to_expiry=10,
            open_interest=4000,
            option_type=OptionType.CALL,
            underlying_price=f.price("24000"),
            greeks=Greeks(
                model="bs",
                calculation_version="1",
                converged=True,
                delta=Decimal("0.25"),
            ),
        ),
    }
    payload.update(overrides)
    return f.snapshot(**payload)


def credit_spread_intent(**overrides: object) -> TradeIntent:
    snap = short_snapshot()
    payload = {
        "snapshot_id": snap.snapshot_id,
        "setup_code": "BEAR_CALL_CREDIT",
        "requested_risk": f.money("4000"),
        "estimated_max_loss": f.money("6000"),
        "legs": (
            IntentLeg(
                leg_id="short",
                contract=short_contract(),
                side=Side.SELL,
                ratio=1,
            ),
            IntentLeg(
                leg_id="long",
                contract=long_contract(),
                side=Side.BUY,
                ratio=1,
            ),
        ),
    }
    payload.update(overrides)
    return f.intent(**payload)


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
def engine() -> CreditSpreadSizingEngine:
    return CreditSpreadSizingEngine()


def _sizing_request(**overrides: object) -> SizingRequest:
    short_snap = short_snapshot()
    portfolio = f.portfolio_snapshot()
    limits = build_sizing_limits(
        portfolio,
        ACCOUNT_CONFIG.config.risk,
        RISK_POLICY.config,
        "positional_index_options_poc",
        config_version=ACCOUNT_CONFIG.version,
    )
    limits = limits.model_copy(update={"max_loss_per_trade": f.money("20000")})
    return f.sizing_request(
        intent=credit_spread_intent(snapshot_id=short_snap.snapshot_id),
        feature_snapshot=short_snap,
        portfolio_snapshot=portfolio,
        limits=limits,
        **overrides,
    )


class TestCreditSpreadDetection:
    def test_recognizes_bear_call_credit_spread(self) -> None:
        assert is_credit_spread(credit_spread_intent())


class TestCreditSpreadSizing:
    def test_sizes_from_wing_loss_minus_credit(
        self,
        engine: CreditSpreadSizingEngine,
        broker: PaperBroker,
    ) -> None:
        """Max loss equals spread width minus net credit plus buffers."""
        request = _sizing_request()
        result = engine.size(
            request,
            {"short": short_snapshot(), "long": long_snapshot()},
            RISK_POLICY.config,
            broker,
            account_id="ACC-PAPER-1",
            account_risk=ACCOUNT_CONFIG.config.risk,
            preview_request_id="MARGIN-CS-1",
            lot_size=LOT_SIZE,
        )
        assert result.approved_lots >= 1
        assert result.recalculated_max_loss > f.money("0")
        assert result.recalculated_max_loss != request.intent.estimated_max_loss
        assert result.net_credit_per_unit == f.price("24.95")
