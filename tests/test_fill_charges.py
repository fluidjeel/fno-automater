"""Durable per-fill charges, conservative net and restart reconstruction."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import tests.factories as f
from tests.structures import (
    open_bull_call_debit,
    open_bull_put_credit,
    open_call_butterfly,
    open_iron_condor,
)
from tests.test_audit_remediation import NOW, _close_open_position
from tests.test_four_mode_session_integration import _runner
from trading.config.charge_policy import load_charge_policy
from tests.test_paper_runner import ROOT
from trading.config.risk_policy import load_risk_policy
from trading.domain.clock import FrozenClock
from trading.domain.enums import OrderState, ReasonCode
from trading.domain.primitives import Currency
from trading.portfolio.campaign_drawdown import trade_accounting
from trading.portfolio.fill_charge_recorder import record_fill_charge
from trading.portfolio.fill_ledger import (
    index_fill_charges,
    index_order_events,
    order_fill_dedupe_key,
    trade_confirmed_charges,
    trade_fill_cash_flow,
)
from trading.portfolio.conservative_net import conservative_realized_net
from trading.risk.mode_ledger import FourModeBook
from trading.storage.trading_store import TradingStore


def _charges_for_trade(store: TradingStore, trade_id: str) -> Decimal:
    orders = index_order_events(store)
    charges = index_fill_charges(store)
    return trade_confirmed_charges(
        trade_id, orders, charges, Currency.INR
    ).amount


class TestFillChargesByStructure:
    def test_debit_spread_persists_charge_components(self, tmp_path: Path) -> None:
        clock = FrozenClock(NOW + timedelta(seconds=60))
        store = TradingStore.open(tmp_path / "debit.sqlite", clock=clock)
        policy = load_charge_policy()
        try:
            runner, position, snapshots = open_bull_call_debit(store, clock)
            _close_open_position(runner, position, snapshots)
            charges = store.list_fill_charges()
            assert charges
            sample = charges[0]
            assert sample.policy_version == policy.config.policy_version
            assert sample.inputs.filled_quantity > 0
            assert sample.components.brokerage.amount > 0
            assert sample.total_charges.amount == sample.components.total.amount
            assert _charges_for_trade(store, position.trade_id) > 0
        finally:
            store.close()

    def test_credit_spread_records_entry_and_exit_charges_once(
        self, tmp_path: Path
    ) -> None:
        clock = FrozenClock(NOW + timedelta(seconds=60))
        store = TradingStore.open(tmp_path / "credit.sqlite", clock=clock)
        try:
            runner, position, snapshots = open_bull_put_credit(store, clock)
            _close_open_position(runner, position, snapshots)
            orders = index_order_events(store)
            trade_orders = [
                event
                for event in orders.values()
                if event.identity.trade_id == position.trade_id
            ]
            charge_keys = {order_fill_dedupe_key(event) for event in trade_orders}
            persisted = index_fill_charges(store)
            assert charge_keys.issubset(persisted.keys())
            assert len(persisted) == len(charge_keys)
        finally:
            store.close()

    def test_iron_condor_multileg_charges(self, tmp_path: Path) -> None:
        clock = FrozenClock(NOW + timedelta(seconds=60))
        store = TradingStore.open(tmp_path / "condor.sqlite", clock=clock)
        try:
            runner, position, snapshots = open_iron_condor(store, clock)
            _close_open_position(runner, position, snapshots)
            assert len(store.list_fill_charges()) >= 4
            assert _charges_for_trade(store, position.trade_id) > 0
        finally:
            store.close()

    def test_call_butterfly_multileg_charges(self, tmp_path: Path) -> None:
        clock = FrozenClock(NOW + timedelta(seconds=60))
        store = TradingStore.open(tmp_path / "fly.sqlite", clock=clock)
        try:
            runner, position, snapshots = open_call_butterfly(store, clock)
            _close_open_position(runner, position, snapshots)
            assert len(store.list_fill_charges()) >= 3
        finally:
            store.close()


class TestFillChargeSafety:
    def test_rejected_fill_has_no_charge_row(self, tmp_path: Path) -> None:
        clock = FrozenClock(NOW + timedelta(seconds=60))
        store = TradingStore.open(tmp_path / "reject.sqlite", clock=clock)
        policy = load_charge_policy()
        try:
            rejected = f.order_event(
                state=OrderState.REJECTED,
                filled_quantity=0,
                average_fill_price=None,
                reason_code=ReasonCode.BROKER_REJECTED,
            )
            assert (
                record_fill_charge(
                    store,
                    rejected,
                    policy=policy.config,
                    contracts_per_lot=75,
                    recorded_at=clock.now_utc(),
                )
                is None
            )
            assert store.list_fill_charges() == ()
        finally:
            store.close()

    def test_replay_does_not_double_charge(self, tmp_path: Path) -> None:
        clock = FrozenClock(NOW + timedelta(seconds=60))
        store = TradingStore.open(tmp_path / "replay.sqlite", clock=clock)
        policy = load_charge_policy()
        try:
            runner, position, snapshots = open_bull_put_credit(store, clock)
            _close_open_position(runner, position, snapshots)
            before = _charges_for_trade(store, position.trade_id)
            orders = index_order_events(store)
            for event in orders.values():
                if event.identity.trade_id != position.trade_id:
                    continue
                record_fill_charge(
                    store,
                    event,
                    policy=policy.config,
                    contracts_per_lot=75,
                    recorded_at=clock.now_utc(),
                )
            after = _charges_for_trade(store, position.trade_id)
            assert after == before
        finally:
            store.close()

    def test_partial_fill_charged_once(self, tmp_path: Path) -> None:
        clock = FrozenClock(NOW + timedelta(seconds=60))
        store = TradingStore.open(tmp_path / "partial.sqlite", clock=clock)
        policy = load_charge_policy()
        try:
            partial = f.order_event(
                state=OrderState.PARTIAL,
                filled_quantity=37,
                average_fill_price=f.price("50.00"),
            )
            filled = partial.model_copy(
                update={
                    "event_id": "EVT-FILLED",
                    "state": OrderState.FILLED,
                    "filled_quantity": 75,
                }
            )
            record_fill_charge(
                store,
                partial,
                policy=policy.config,
                contracts_per_lot=75,
                recorded_at=clock.now_utc(),
            )
            record_fill_charge(
                store,
                filled,
                policy=policy.config,
                contracts_per_lot=75,
                recorded_at=clock.now_utc(),
            )
            rows = store.list_fill_charges()
            assert len(rows) == 1
            assert rows[0].inputs.filled_quantity == 75
        finally:
            store.close()


class TestFillChargeRestart:
    def test_restart_reconstructs_gross_charges_and_net(self, tmp_path: Path) -> None:
        clock = FrozenClock(NOW + timedelta(seconds=60))
        store = TradingStore.open(tmp_path / "restart.sqlite", clock=clock)
        risk = load_risk_policy(ROOT / "config" / "risk.yaml")
        try:
            runner, position, snapshots = open_bull_call_debit(store, clock)
            _close_open_position(runner, position, snapshots)
            lifecycle = store.get_position_lifecycle(position.trade_id)
            assert lifecycle is not None
            orders = index_order_events(store)
            charges = index_fill_charges(store)
            before = trade_accounting(
                lifecycle,
                orders,
                charges,
                charges_per_lot=risk.config.charges_per_lot.to_money(),
            )
            clock.set(clock.now_utc() + timedelta(seconds=30))
            resumed = _runner(store, clock)
            resumed.recover_lifecycle()
            orders_after = index_order_events(store)
            charges_after = index_fill_charges(store)
            after = trade_accounting(
                lifecycle,
                orders_after,
                charges_after,
                charges_per_lot=risk.config.charges_per_lot.to_money(),
            )
            assert after.realized_gross == before.realized_gross
            assert after.confirmed_charges == before.confirmed_charges
            assert after.realized_net == before.realized_net
            book = FourModeBook.reconstruct_from_store(
                store, clock.now_utc().date()
            )
            ledger = book.get_ledger(lifecycle.intent.mode_id)  # type: ignore[arg-type]
            assert ledger.realized_gross_pnl_today == before.realized_gross
            assert ledger.realized_pnl_today == conservative_realized_net(
                before.realized_gross,
                confirmed_charges=before.confirmed_charges,
                estimated_charges=before.estimated_charges,
            )
        finally:
            store.close()
