"""Legacy trading-store migrations (pre-900293f schema)."""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from tests.fixtures.legacy_trading_builder import NOW, build_legacy_trading_db
from trading.config.risk_policy import load_risk_policy
from trading.domain.clock import WallClock
from trading.domain.enums import ModeId
from trading.portfolio.campaign_drawdown import CampaignLedger, trade_accounting
from trading.portfolio.fill_ledger import index_fill_charges, index_order_events
from trading.risk.mode_ledger import FourModeBook
from trading.storage.schema_migration import (
    MIGRATION_PRE_900293F_FILL_CHARGES,
    MIGRATION_PRE_900293F_SCHEMA,
    apply_data_migrations,
    apply_schema_migrations,
    capture_row_counts,
    is_migration_applied,
)
from trading.storage.trading_store import TradingStore

ORACLE_FIXTURE = (
    Path(__file__).resolve().parent / "fixtures" / "oracle_trading_pre_900293f.sqlite"
)


@pytest.fixture
def clock() -> WallClock:
    return WallClock()


def _copy_oracle_fixture(tmp_path: Path) -> Path:
    assert ORACLE_FIXTURE.is_file(), "Oracle backup fixture missing"
    target = tmp_path / "oracle_copy.sqlite"
    shutil.copy2(ORACLE_FIXTURE, target)
    return target


class TestOracleBackupMigration:
    """Migrate a copy of the backed-up Oracle database; never the only backup."""

    def test_preserves_event_row_counts(self, tmp_path: Path, clock: WallClock) -> None:
        db_path = _copy_oracle_fixture(tmp_path)
        before = capture_row_counts(sqlite3.connect(db_path))
        store = TradingStore.open(db_path, clock=clock)
        after = capture_row_counts(sqlite3.connect(db_path))
        store.close()
        assert before.trading_events == 2374
        assert after.trading_events == before.trading_events
        assert after.position_lifecycle == before.position_lifecycle
        assert after.reservations == before.reservations
        assert after.idempotency_keys == before.idempotency_keys
        assert is_migration_applied(sqlite3.connect(db_path), MIGRATION_PRE_900293F_SCHEMA)
        assert is_migration_applied(
            sqlite3.connect(db_path), MIGRATION_PRE_900293F_FILL_CHARGES
        )

    def test_second_open_is_no_op(self, tmp_path: Path, clock: WallClock) -> None:
        db_path = _copy_oracle_fixture(tmp_path)
        first = TradingStore.open(db_path, clock=clock)
        first.close()
        counts_after_first = capture_row_counts(sqlite3.connect(db_path))
        store = TradingStore.open(db_path, clock=clock)
        connection = sqlite3.connect(db_path)
        assert apply_schema_migrations(connection, applied_at=NOW) == ()
        assert apply_data_migrations(store) == ()
        connection.close()
        store.close()
        counts_after_second = capture_row_counts(sqlite3.connect(db_path))
        assert counts_after_second == counts_after_first


class TestSyntheticLegacyTradingMigration:
    """Full trading-state migration on a constructed pre-900293f schema."""

    def test_lifecycle_reservations_campaigns_and_pnl(
        self, tmp_path: Path, clock: WallClock
    ) -> None:
        db_path = tmp_path / "synthetic_legacy.sqlite"
        snapshot = build_legacy_trading_db(db_path)
        before = snapshot.row_counts
        store = TradingStore.open(db_path, clock=clock)
        after = capture_row_counts(sqlite3.connect(db_path))
        assert after.trading_events == before.trading_events + 2  # durable FILL_CHARGE rows
        assert after.position_lifecycle == before.position_lifecycle
        assert after.reservations == before.reservations
        assert after.campaign_ledger == before.campaign_ledger
        lifecycle = store.get_position_lifecycle(snapshot.trade_id)
        assert lifecycle is not None
        assert lifecycle.position.state.value == "CLOSED"
        orders = index_order_events(store)
        charges = index_fill_charges(store)
        risk = load_risk_policy(
            Path(__file__).resolve().parents[1] / "config" / "risk.yaml"
        )
        accounting = trade_accounting(
            lifecycle,
            orders,
            charges,
            charges_per_lot=risk.config.charges_per_lot.to_money(),
        )
        assert accounting.realized_gross.amount == snapshot.gross_amount
        assert len(charges) == 2
        assert accounting.confirmed_charges.amount > 0
        assert accounting.realized_net.amount < accounting.realized_gross.amount
        campaign = CampaignLedger.reconstruct_from_store(store).get(snapshot.campaign_id)
        assert campaign is not None
        assert campaign.cumulative_realized_gross.amount == snapshot.gross_amount
        book = FourModeBook.reconstruct_from_store(store, NOW.date())
        ledger = book.get_ledger(ModeId.M3_TACTICAL_POSITIONAL)
        assert ledger.realized_gross_pnl_today.amount == snapshot.gross_amount
        store.close()

    def test_fill_charge_dedup_on_second_migration(self, tmp_path: Path, clock: WallClock) -> None:
        db_path = tmp_path / "synthetic_legacy.sqlite"
        build_legacy_trading_db(db_path)
        store = TradingStore.open(db_path, clock=clock)
        charges_first = len(store.list_fill_charges())
        assert charges_first == 2
        connection = sqlite3.connect(db_path)
        assert apply_schema_migrations(connection, applied_at=NOW) == ()
        assert apply_data_migrations(store) == ()
        connection.close()
        assert len(store.list_fill_charges()) == charges_first
        store.close()
