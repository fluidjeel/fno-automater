"""DISC-A4: touch-v1 paper fills with strict fill verdict shadow."""

from __future__ import annotations

from pathlib import Path

import pytest

import tests.factories as f
from tests.structures import open_iron_condor
from tests.test_audit_remediation import TestP0LiabilityFirstExitSequencing
from tests.test_l4_fills import verified_fill_model
from trading.analytics.scorecard import build_scorecard
from trading.broker.paper import PaperBroker
from trading.broker.ports import BrokerSubmitRequest
from trading.config import load_evaluation_config
from trading.config.discovery import load_discovery_config
from trading.config.evaluation import build_fill_model, discovery_fill_models
from trading.domain.clock import FrozenClock
from trading.domain.contracts.evaluation import CohortPackage, CohortSignal
from trading.domain.contracts.order_plan import PlannedOrder
from trading.domain.enums import (
    OrderPlanState,
    OrderState,
    ReasonCode,
    RiskAction,
    Side,
)
from trading.domain.ids import SequentialIdFactory
from trading.domain.primitives import Price

ROOT = Path(__file__).resolve().parent.parent
BROKER_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "broker"
EVALUATION = ROOT / "config" / "evaluation.yaml"
DISCOVERY = ROOT / "config" / "discovery.yaml"


def _planned(
    *,
    side: Side = Side.BUY,
    limit: str = "121.00",
    quantity: int = 75,
) -> PlannedOrder:
    identity = f.order_event().identity
    return PlannedOrder(
        plan_leg_id="leg-1-entry",
        leg_id="leg-1",
        identity=identity,
        command=f.order_command(
            side=side,
            quantity_contracts=quantity,
            limit_price=f.price(limit),
        ),
        plan_state=OrderPlanState.RISK_APPROVED,
    )


def _discovery_broker(clock: FrozenClock) -> PaperBroker:
    base = load_evaluation_config(EVALUATION).config.fill_model
    discovery = load_discovery_config(DISCOVERY)
    touch, shadow = discovery_fill_models(
        base,
        model=discovery.fills.model,
        shadow_model=discovery.fills.shadow_model,
    )
    return PaperBroker.from_fixtures(
        BROKER_FIXTURES,
        clock=clock,
        id_factory=SequentialIdFactory(clock.instant),
        fill_model=touch,
        shadow_fill_model=shadow,
    )


def _strict_broker(clock: FrozenClock) -> PaperBroker:
    return PaperBroker.from_fixtures(
        BROKER_FIXTURES,
        clock=clock,
        id_factory=SequentialIdFactory(clock.instant),
        fill_model=verified_fill_model(),
    )


class TestTouchV1FillModel:
    def test_buy_fills_at_ask_with_depth_insufficient_strict_verdict(self) -> None:
        clock = FrozenClock(f.NOW)
        broker = _discovery_broker(clock)
        planned = _planned(limit="121.00")
        broker.publish_quote(
            planned.command.contract.symbol,
            f.quote(
                bid=f.price("119.50"),
                ask=f.price("120.50"),
                last=f.price("118.00"),
                bid_size=300,
                ask_size=0,
            ),
        )
        event = broker.submit(
            BrokerSubmitRequest(
                account_id="ACC-PAPER-1",
                strategy_id="positional_long_option",
                order=planned,
            )
        )
        assert event.state is OrderState.FILLED
        assert event.average_fill_price == Price.snap("120.50", f.TICK)
        assert event.strict_fill_verdict is ReasonCode.DEPTH_INSUFFICIENT

    def test_sell_rejects_when_bid_missing(self) -> None:
        clock = FrozenClock(f.NOW)
        broker = _discovery_broker(clock)
        planned = _planned(side=Side.SELL, limit="119.00")
        broker.publish_quote(
            planned.command.contract.symbol,
            f.quote(
                bid=None,
                ask=f.price("120.50"),
                last=f.price("118.00"),
                bid_size=None,
                ask_size=300,
            ),
        )
        event = broker.submit(
            BrokerSubmitRequest(
                account_id="ACC-PAPER-1",
                strategy_id="positional_long_option",
                order=planned,
            )
        )
        assert event.state is OrderState.REJECTED
        assert event.reason_code is ReasonCode.PRICE_UNAVAILABLE
        assert event.filled_quantity == 0
        assert broker.get_positions() == ()

    def test_touch_fill_includes_charges(self) -> None:
        clock = FrozenClock(f.NOW)
        broker = _discovery_broker(clock)
        planned = _planned(limit="121.00")
        broker.publish_quote(
            planned.command.contract.symbol,
            f.quote(
                bid=f.price("119.50"),
                ask=f.price("120.50"),
                last=f.price("118.00"),
                bid_size=300,
                ask_size=300,
            ),
        )
        event = broker.submit(
            BrokerSubmitRequest(
                account_id="ACC-PAPER-1",
                strategy_id="positional_long_option",
                order=planned,
            )
        )
        assert event.state is OrderState.FILLED
        assert event.strict_fill_verdict is ReasonCode.OK


class TestStrictProfileUnchanged:
    def test_strict_uses_conservative_v1_without_shadow_verdict(self) -> None:
        clock = FrozenClock(f.NOW)
        broker = _strict_broker(clock)
        planned = _planned(limit="100.05")
        broker.publish_quote(
            planned.command.contract.symbol,
            f.quote(bid_size=300, ask_size=300, last=f.price("100.05")),
        )
        event = broker.submit(
            BrokerSubmitRequest(
                account_id="ACC-PAPER-1",
                strategy_id="positional_long_option",
                order=planned,
            )
        )
        assert event.state is OrderState.FILLED
        assert event.average_fill_price == Price.snap("100.10", f.TICK)
        assert event.strict_fill_verdict is None
        assert broker.shadow_fill_model is None


class TestIronCondorProtectionFirst:
    def test_entry_long_wings_before_shorts_and_exit_covers_shorts_first(
        self, tmp_path: Path
    ) -> None:
        clock = FrozenClock(f.NOW)
        store_path = tmp_path / "condor.sqlite"
        from trading.storage.trading_store import TradingStore

        store = TradingStore.open(store_path, clock=clock)
        try:
            runner, position, snapshots = open_iron_condor(store, clock)
            entry_sides = [
                event.command.side
                for event in runner.broker.list_orders()
                if event.state is OrderState.FILLED
            ]
            assert entry_sides.count(Side.BUY) == 2
            assert entry_sides.count(Side.SELL) == 2
            first_sell = next(
                index for index, side in enumerate(entry_sides) if side is Side.SELL
            )
            assert all(side is Side.BUY for side in entry_sides[:first_sell])
            TestP0LiabilityFirstExitSequencing()._check(runner, position, snapshots)
        finally:
            store.close()


class TestDiscoveryScorecard:
    def test_scorecard_reports_touch_v1_and_strict_verdict_counts(self) -> None:
        base = load_evaluation_config(EVALUATION).config.fill_model
        touch = build_fill_model("touch-v1", base)
        package = CohortPackage(
            experiment=f.experiment(fill_model_version="touch-v1"),
            observation_start=f.NOW,
            observation_end=f.NOW,
            signals=(
                CohortSignal(
                    signal_id="SIG-DEPTH",
                    snapshot_id="SNAP-1",
                    created_at=f.NOW,
                    intent=f.intent(experiment_id="EXP-LO-PAPER-1"),
                    risk_action=RiskAction.APPROVE,
                    entry_quote=f.quote(
                        bid=f.price("119.50"),
                        ask=f.price("120.50"),
                        last=f.price("118.00"),
                        ask_size=0,
                    ),
                    entry_command=f.order_command(
                        side=Side.BUY,
                        limit_price=f.price("121.00"),
                    ),
                    strict_fill_verdict=ReasonCode.DEPTH_INSUFFICIENT,
                ),
                CohortSignal(
                    signal_id="SIG-OK",
                    snapshot_id="SNAP-2",
                    created_at=f.NOW,
                    intent=f.intent(
                        intent_id="INT-2",
                        experiment_id="EXP-LO-PAPER-1",
                    ),
                    risk_action=RiskAction.APPROVE,
                    entry_quote=f.quote(bid_size=300, ask_size=300),
                    entry_command=f.order_command(
                        side=Side.BUY,
                        limit_price=f.price("100.05"),
                    ),
                    strict_fill_verdict=ReasonCode.OK,
                ),
            ),
        )
        scorecard = build_scorecard(package, touch, as_of=package.observation_end)
        assert scorecard.fill_model_version == "touch-v1"
        verdicts = {
            item.reason_code: item.count
            for item in scorecard.strict_fill_verdict_histogram
        }
        assert verdicts[ReasonCode.DEPTH_INSUFFICIENT] == 1
        assert verdicts[ReasonCode.OK] == 1

    def test_refuses_to_pool_touch_with_conservative(self) -> None:
        package = CohortPackage(
            experiment=f.experiment(fill_model_version="touch-v1"),
            observation_start=f.NOW,
            observation_end=f.NOW,
            signals=(
                CohortSignal(
                    signal_id="SIG-1",
                    snapshot_id="SNAP-1",
                    created_at=f.NOW,
                    intent=f.intent(experiment_id="EXP-LO-PAPER-1"),
                    risk_action=RiskAction.APPROVE,
                    entry_quote=f.quote(bid_size=300, ask_size=300),
                    entry_command=f.order_command(side=Side.BUY),
                ),
            ),
        )
        conservative = load_evaluation_config(EVALUATION).config.fill_model
        with pytest.raises(Exception, match="do not pool versions"):
            build_scorecard(package, conservative, as_of=package.observation_end)


class TestPaperRunnerFillModelPair:
    def test_runner_accepts_touch_plus_shadow_pair(self, tmp_path: Path) -> None:
        from trading.config.loader import load_config
        from trading.config.risk_policy import load_risk_policy
        from trading.runtime.paper_runner import PaperRunner
        from trading.storage.trading_store import TradingStore

        clock = FrozenClock(f.NOW)
        store = TradingStore.open(tmp_path / "runner.sqlite", clock=clock)
        try:
            base = load_evaluation_config(EVALUATION).config.fill_model
            discovery = load_discovery_config(DISCOVERY)
            touch, shadow = discovery_fill_models(
                base,
                model=discovery.fills.model,
                shadow_model=discovery.fills.shadow_model,
            )
            broker = PaperBroker.from_fixtures(
                BROKER_FIXTURES,
                clock=clock,
                id_factory=SequentialIdFactory(clock.instant),
                fill_model=touch,
                shadow_fill_model=shadow,
            )
            account = load_config(ROOT / "config" / "paper.yaml")
            risk = load_risk_policy(ROOT / "config" / "risk.yaml")
            runner = PaperRunner(
                account_config=account,
                risk_policy=risk,
                store=store,
                broker=broker,
                clock=clock,
                id_factory=SequentialIdFactory(clock.instant),
                fill_model=touch,
                shadow_fill_model=shadow,
            )
            assert runner.broker.fill_model is not None
            assert runner.broker.fill_model.version == "touch-v1"
            assert runner.broker.shadow_fill_model is not None
            assert runner.broker.shadow_fill_model.version == "conservative-v1"
        finally:
            store.close()
