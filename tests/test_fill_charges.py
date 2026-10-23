"""Durable per-fill charges, conservative net and restart reconstruction."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import tests.factories as f
from tests.structures import open_m2_long_call
from tests.test_audit_remediation import NOW, _close_open_position
from tests.paper_session_helpers import _runner
from tests.test_paper_runner import ROOT
from trading.domain.clock import FrozenClock
from trading.domain.primitives import Currency
from trading.portfolio.fill_ledger import index_fill_charges, index_order_events, trade_confirmed_charges
from trading.domain.contracts.mode_policy import load_modes_config
from trading.risk.mode_ledger import FourModeBook
from trading.storage.trading_store import TradingStore


def _charges_for_trade(store: TradingStore, trade_id: str) -> Decimal:
    orders = index_order_events(store)
    charges = index_fill_charges(store)
    return trade_confirmed_charges(trade_id, orders, charges, Currency.INR).amount


def test_fill_charge_persisted_and_reconstructed(tmp_path: Path) -> None:
    clock = FrozenClock(NOW + timedelta(seconds=60))
    store = TradingStore.open(tmp_path / "charges.sqlite", clock=clock)
    try:
        runner, position, snapshots = open_m2_long_call(store, clock)
        _close_open_position(runner, position, snapshots)
        trade_id = position.trade_id
        assert _charges_for_trade(store, trade_id) > Decimal(0)
    finally:
        store.close()


def test_mode_ledger_reserved_after_restart(tmp_path: Path) -> None:
    clock = FrozenClock(NOW + timedelta(seconds=60))
    store = TradingStore.open(tmp_path / "ledger.sqlite", clock=clock)
    try:
        open_m2_long_call(store, clock)
        book = FourModeBook.reconstruct_from_store(
            store,
            clock.now_utc().date(),
            modes_config=load_modes_config(ROOT / "config" / "modes.yaml"),
        )
        assert book.total_open_risk().amount > Decimal(0)
    finally:
        store.close()
