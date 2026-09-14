"""Debit spread sizing engine (L2-011).

Invariant 4: Layer 2 recalculates defined max loss from net debit at decision time.
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
from trading.risk.sizing.debit_spread import DebitSpreadSizingEngine, is_debit_spread

ROOT = Path(__file__).resolve().parent.parent
RISK_POLICY = load_risk_policy(ROOT / "config" / "risk.yaml")
ACCOUNT_CONFIG = load_config(ROOT / "config" / "base.yaml")
BROKER_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "broker"
NOW = f.NOW
LOT_SIZE = LotSize(75)


def long_contract() -> ContractRef:
    return f.option_contract(symbol="NIFTY26SEP24000CE", strike=Decimal("24000"))


def short_contract() -> ContractRef:
    return f.option_contract(
        symbol="NIFTY26SEP24200CE",
        strike=Decimal("24200"),
    )


def long_snapshot(**overrides: object) -> FeatureSnapshot:
    payload: dict[str, object] = {
        "contract": long_contract(),
        "market": f.quote(
            bid=f.price("91.95"),
            ask=f.price("92.00"),
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
                delta=Decimal("0.55"),
            ),
        ),
    }
    payload.update(overrides)
    return f.snapshot(**payload)


def short_snapshot(**overrides: object) -> FeatureSnapshot:
    payload: dict[str, object] = {
        "contract": short_contract(),
        "market": f.quote(
            bid=f.price("44.95"),
            ask=f.price("45.00"),
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
                delta=Decimal("0.35"),
            ),
        ),
    }
    payload.update(overrides)
    return f.snapshot(**payload)


def debit_spread_intent(**overrides: object) -> TradeIntent:
    snap = long_snapshot()
    payload = {
        "snapshot_id": snap.snapshot_id,
        "setup_code": "BULL_CALL_DEBIT",
        "requested_risk": f.money("4000"),
        "estimated_max_loss": f.money("6000"),
        "legs": (
            IntentLeg(
                leg_id="long",
                contract=long_contract(),
                side=Side.BUY,
                ratio=1,
            ),
            IntentLeg(
                leg_id="short",
                contract=short_contract(),
                side=Side.SELL,
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
def engine() -> DebitSpreadSizingEngine:
    return DebitSpreadSizingEngine()


def _sizing_request(**overrides: object) -> SizingRequest:
    long_snap = long_snapshot()
    portfolio = f.portfolio_snapshot()
    limits = build_sizing_limits(
        portfolio,
        ACCOUNT_CONFIG.config.risk,
        RISK_POLICY.config,
        "positional_index_options_poc",
        config_version=ACCOUNT_CONFIG.version,
    )
    limits = limits.model_copy(update={"max_loss_per_trade": f.money("10000")})
    return f.sizing_request(
        intent=debit_spread_intent(snapshot_id=long_snap.snapshot_id),
        feature_snapshot=long_snap,
        portfolio_snapshot=portfolio,
        limits=limits,
        **overrides,
    )


class TestDebitSpreadDetection:
    def test_recognizes_bull_call_debit_spread(self) -> None:
        assert is_debit_spread(debit_spread_intent())

    def test_rejects_single_leg(self) -> None:
        assert not is_debit_spread(f.intent())


class TestDebitSpreadSizing:
    def test_sizes_from_net_debit_defined_max_loss(
        self,
        engine: DebitSpreadSizingEngine,
        broker: PaperBroker,
    ) -> None:
        """Max loss equals net debit plus buffers, not strategy estimate."""
        request = _sizing_request()
        long_snap = long_snapshot()
        short_snap = short_snapshot()
        result = engine.size(
            request,
            {"long": long_snap, "short": short_snap},
            RISK_POLICY.config,
            broker,
            account_id="ACC-PAPER-1",
            account_risk=ACCOUNT_CONFIG.config.risk,
            preview_request_id="MARGIN-DS-1",
            lot_size=LOT_SIZE,
        )
        assert result.approved_lots >= 1
        assert result.recalculated_max_loss > f.money("0")
        assert result.recalculated_max_loss != request.intent.estimated_max_loss
        assert result.net_debit_per_unit == f.price("47.05")

    def test_zero_lots_when_net_debit_exceeds_spread_width(
        self,
        engine: DebitSpreadSizingEngine,
        broker: PaperBroker,
    ) -> None:
        long_snap = long_snapshot(
            market=f.quote(
                bid=f.price("249.00"),
                ask=f.price("250.50"),
                bid_size=300,
                ask_size=300,
            ),
        )
        short_snap = short_snapshot()
        request = _sizing_request()
        with pytest.raises(ValueError, match="spread width"):
            engine.size(
                request,
                {"long": long_snap, "short": short_snap},
                RISK_POLICY.config,
                broker,
                account_id="ACC-PAPER-1",
                account_risk=ACCOUNT_CONFIG.config.risk,
                preview_request_id="MARGIN-DS-2",
                lot_size=LOT_SIZE,
            )
