"""Iron condor sizing engine (L2-014).

Invariant 4: Layer 2 recalculates wing loss minus credit at decision time.
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
from trading.risk.sizing.iron_condor import IronCondorSizingEngine, is_iron_condor

ROOT = Path(__file__).resolve().parent.parent
RISK_POLICY = load_risk_policy(ROOT / "config" / "risk.yaml")
ACCOUNT_CONFIG = load_config(ROOT / "config" / "base.yaml")
BROKER_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "broker"
NOW = f.NOW
LOT_SIZE = LotSize(75)


def short_call_contract() -> ContractRef:
    return f.option_contract(symbol="NIFTY26SEP24500CE", strike=Decimal("24500"))


def long_call_contract() -> ContractRef:
    return f.option_contract(symbol="NIFTY26SEP24700CE", strike=Decimal("24700"))


def short_put_contract() -> ContractRef:
    return f.option_contract(
        symbol="NIFTY26SEP23500PE",
        strike=Decimal("23500"),
        option_type=OptionType.PUT,
    )


def long_put_contract() -> ContractRef:
    return f.option_contract(
        symbol="NIFTY26SEP23300PE",
        strike=Decimal("23300"),
        option_type=OptionType.PUT,
    )


def _leg_snapshot(
    contract: ContractRef,
    *,
    bid: str,
    ask: str,
    option_type: OptionType,
    delta: str,
) -> FeatureSnapshot:
    return f.snapshot(
        contract=contract,
        market=f.quote(
            bid=f.price(bid),
            ask=f.price(ask),
            bid_size=300,
            ask_size=300,
        ),
        derivatives=DerivativesContext(
            days_to_expiry=10,
            open_interest=5000,
            option_type=option_type,
            underlying_price=f.price("24000"),
            greeks=Greeks(
                model="bs",
                calculation_version="1",
                converged=True,
                delta=Decimal(delta),
            ),
        ),
    )


def iron_condor_intent(**overrides: object) -> TradeIntent:
    snap = _leg_snapshot(
        short_call_contract(),
        bid="29.95",
        ask="30.00",
        option_type=OptionType.CALL,
        delta="0.20",
    )
    payload = {
        "snapshot_id": snap.snapshot_id,
        "setup_code": "IRON_CONDOR",
        "requested_risk": f.money("5000"),
        "estimated_max_loss": f.money("8000"),
        "legs": (
            IntentLeg(
                leg_id="short_call",
                contract=short_call_contract(),
                side=Side.SELL,
                ratio=1,
            ),
            IntentLeg(
                leg_id="long_call",
                contract=long_call_contract(),
                side=Side.BUY,
                ratio=1,
            ),
            IntentLeg(
                leg_id="short_put",
                contract=short_put_contract(),
                side=Side.SELL,
                ratio=1,
            ),
            IntentLeg(
                leg_id="long_put",
                contract=long_put_contract(),
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
def engine() -> IronCondorSizingEngine:
    return IronCondorSizingEngine()


def _leg_snapshots() -> dict[str, FeatureSnapshot]:
    return {
        "short_call": _leg_snapshot(
            short_call_contract(),
            bid="29.95",
            ask="30.00",
            option_type=OptionType.CALL,
            delta="0.20",
        ),
        "long_call": _leg_snapshot(
            long_call_contract(),
            bid="14.95",
            ask="15.00",
            option_type=OptionType.CALL,
            delta="0.10",
        ),
        "short_put": _leg_snapshot(
            short_put_contract(),
            bid="27.95",
            ask="28.00",
            option_type=OptionType.PUT,
            delta="-0.18",
        ),
        "long_put": _leg_snapshot(
            long_put_contract(),
            bid="12.95",
            ask="13.00",
            option_type=OptionType.PUT,
            delta="-0.08",
        ),
    }


def _sizing_request(**overrides: object) -> SizingRequest:
    legs = _leg_snapshots()
    portfolio = f.portfolio_snapshot()
    limits = build_sizing_limits(
        portfolio,
        ACCOUNT_CONFIG.config.risk,
        RISK_POLICY.config,
        "positional_index_options_poc",
        config_version=ACCOUNT_CONFIG.version,
    )
    limits = limits.model_copy(update={"max_loss_per_trade": f.money("25000")})
    return f.sizing_request(
        intent=iron_condor_intent(snapshot_id=legs["short_call"].snapshot_id),
        feature_snapshot=legs["short_call"],
        portfolio_snapshot=portfolio,
        limits=limits,
        **overrides,
    )


class TestIronCondorDetection:
    def test_recognizes_four_leg_condor(self) -> None:
        assert is_iron_condor(iron_condor_intent())


class TestIronCondorSizing:
    def test_sizes_from_wing_loss_minus_total_credit(
        self,
        engine: IronCondorSizingEngine,
        broker: PaperBroker,
    ) -> None:
        """Max loss equals wing width minus combined credit plus buffers."""
        request = _sizing_request()
        leg_snapshots = _leg_snapshots()
        result = engine.size(
            request,
            leg_snapshots,
            RISK_POLICY.config,
            broker,
            account_id="ACC-PAPER-1",
            account_risk=ACCOUNT_CONFIG.config.risk,
            preview_request_id="MARGIN-IC-1",
            lot_size=LOT_SIZE,
        )
        assert result.approved_lots >= 1
        assert result.recalculated_max_loss > f.money("0")
        assert result.recalculated_max_loss != request.intent.estimated_max_loss
        assert result.wing_width == Decimal("200")
        assert result.net_credit_per_unit == f.price("29.90")
