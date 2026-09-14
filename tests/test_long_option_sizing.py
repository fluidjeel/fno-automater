"""Long call/put sizing engine (L2-006).

Invariant 4: Layer 2 recalculates authoritative max loss at decision time.
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
    SizingRequest,
)
from trading.domain.enums import (
    Exchange,
    InstrumentKind,
    OptionType,
    SizingBindingConstraint,
)
from trading.domain.ids import SequentialIdFactory
from trading.risk.limits import build_sizing_limits
from trading.risk.sizing.long_option import LongOptionSizingEngine

ROOT = Path(__file__).resolve().parent.parent
RISK_POLICY = load_risk_policy(ROOT / "config" / "risk.yaml")
ACCOUNT_CONFIG = load_config(ROOT / "config" / "base.yaml")
BROKER_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "broker"
NOW = f.NOW


def instrument_spec(**overrides: object) -> InstrumentSpec:
    payload: dict[str, object] = {
        "trading_symbol": "NSE:NIFTY26SEP24000CE",
        "exchange": Exchange.NFO,
        "segment": "NSE_FO",
        "underlying": "NIFTY",
        "instrument_kind": InstrumentKind.OPTION,
        "provider_token": "tok-1",
        "exchange_token": 1,
        "lot_size": 75,
        "tick_size": Decimal("0.05"),
        "price_precision": 2,
        "expiry": date(2026, 9, 24),
        "strike": Decimal("24000"),
        "option_type": OptionType.CALL,
        "trading_session": "0915-1530",
        "source": "fixture",
        "verified_at": date(2026, 9, 1),
    }
    payload.update(overrides)
    return InstrumentSpec.model_validate(payload)


def option_snapshot(**overrides: object) -> FeatureSnapshot:
    payload: dict[str, object] = {
        "contract": f.option_contract(),
        "market": f.quote(
            bid=f.price("100.00"),
            ask=f.price("100.05"),
            bid_size=300,
            ask_size=300,
        ),
        "derivatives": DerivativesContext(
            days_to_expiry=10,
            open_interest=5000,
            option_type=OptionType.CALL,
            underlying_price=f.price("24000"),
        ),
    }
    payload.update(overrides)
    return f.snapshot(**payload)


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
def engine() -> LongOptionSizingEngine:
    return LongOptionSizingEngine()


def _sizing_request(**overrides: object) -> SizingRequest:
    snap = option_snapshot()
    portfolio = f.portfolio_snapshot()
    limits = build_sizing_limits(
        portfolio,
        ACCOUNT_CONFIG.config.risk,
        RISK_POLICY.config,
        "positional_index_options_poc",
        config_version=ACCOUNT_CONFIG.version,
    )
    limits = limits.model_copy(
        update={"max_loss_per_trade": f.money("10000")},
    )
    return f.sizing_request(
        intent=f.intent(snapshot_id=snap.snapshot_id),
        feature_snapshot=snap,
        portfolio_snapshot=portfolio,
        limits=limits,
        **overrides,
    )


class TestLongOptionSizing:
    def test_min_formula_binds_risk_term(
        self,
        engine: LongOptionSizingEngine,
        broker: PaperBroker,
        clock: FrozenClock,
    ) -> None:
        """The smallest min() term sets approved lots and binding constraint."""
        request = _sizing_request()
        limits = request.limits.model_copy(
            update={
                "max_loss_per_trade": f.money("8000"),
                "strategy_allocation_remaining": f.money("200000"),
                "margin_available": f.money("700000"),
            }
        )
        request = request.model_copy(update={"limits": limits})
        result = engine.size(
            request,
            instrument_spec(),
            RISK_POLICY.config,
            broker,
            account_id="ACC-PAPER-1",
            account_risk=ACCOUNT_CONFIG.config.risk,
            preview_request_id="MARGIN-1",
        )
        assert result.approved_lots == 1
        assert result.bounds.binding_constraint(1) is SizingBindingConstraint.RISK
        assert result.recalculated_max_loss > f.money("0")
        assert result.recalculated_max_loss != request.intent.estimated_max_loss

    def test_capital_term_binds_when_strategy_budget_is_tight(
        self,
        engine: LongOptionSizingEngine,
        broker: PaperBroker,
    ) -> None:
        """Capital allocation can be the binding min() term."""
        request = _sizing_request()
        limits = request.limits.model_copy(
            update={
                "max_loss_per_trade": f.money("50000"),
                "strategy_allocation_remaining": f.money("7600"),
                "margin_available": f.money("700000"),
                "daily_loss_remaining": f.money("50000"),
            }
        )
        request = request.model_copy(update={"limits": limits})
        result = engine.size(
            request,
            instrument_spec(),
            RISK_POLICY.config,
            broker,
            account_id="ACC-PAPER-1",
            account_risk=ACCOUNT_CONFIG.config.risk,
            preview_request_id="MARGIN-2",
        )
        assert result.approved_lots == 1
        assert result.bounds.binding_constraint(1) is SizingBindingConstraint.CAPITAL

    def test_zero_lots_when_risk_budget_is_insufficient(
        self,
        engine: LongOptionSizingEngine,
        broker: PaperBroker,
    ) -> None:
        request = _sizing_request()
        limits = request.limits.model_copy(
            update={"max_loss_per_trade": f.money("1000")},
        )
        request = request.model_copy(update={"limits": limits})
        result = engine.size(
            request,
            instrument_spec(),
            RISK_POLICY.config,
            broker,
            account_id="ACC-PAPER-1",
            account_risk=ACCOUNT_CONFIG.config.risk,
            preview_request_id="MARGIN-3",
        )
        assert result.approved_lots == 0
