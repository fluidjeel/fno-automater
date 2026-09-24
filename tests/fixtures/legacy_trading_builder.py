"""Build pre-900293f trading.sqlite fixtures for migration tests."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import tests.factories as f
from pydantic import BaseModel

from trading.domain.contracts.campaign import CampaignRecord
from trading.domain.contracts.lifecycle import PositionLifecycleRecord
from trading.domain.enums import (
    HoldingStyle,
    ModeId,
    OrderState,
    ReservationState,
    Side,
    TradeState,
)
from trading.domain.primitives import Currency, Money
from trading.storage.schema_migration import RowCounts, capture_row_counts
from trading.storage.trading_store import TradingEventType

NOW = datetime(2026, 9, 14, 4, 0, tzinfo=UTC)

_LEGACY_SCHEMA = """
CREATE TABLE trading_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    payload TEXT NOT NULL,
    idempotency_key TEXT,
    recorded_at TEXT NOT NULL
);
CREATE TABLE idempotency_keys (
    idempotency_key TEXT PRIMARY KEY,
    owner_ref TEXT NOT NULL,
    registered_at TEXT NOT NULL
);
CREATE TABLE reservations (
    reservation_id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    payload TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE position_lifecycle (
    trade_id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    payload TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE campaign_ledger (
    campaign_id TEXT PRIMARY KEY,
    mode_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


@dataclass(frozen=True, slots=True)
class LegacyTradingSnapshot:
    """Expected reconstruction facts for one synthetic legacy database."""

    row_counts: RowCounts
    trade_id: str
    gross_amount: Decimal
    campaign_id: str
    reservation_id: str


def build_legacy_trading_db(path: Path) -> LegacyTradingSnapshot:
    """Create a pre-900293f store with fills, lifecycle, reservation and campaign."""
    if path.exists():
        path.unlink()
    connection = sqlite3.connect(path)
    connection.executescript(_LEGACY_SCHEMA)
    trade_id = "TRD-LEGACY-1"
    campaign_id = "CAMP-LEGACY-1"
    reservation_id = "RES-LEGACY-1"
    intent = f.intent(intent_id="INT-LEGACY-1", mode_id=ModeId.M3_TACTICAL_POSITIONAL)
    decision = f.risk_decision(
        intent_id="INT-LEGACY-1",
        capital_reservation_id=reservation_id,
    )
    entry_leg = f.position_leg_state(
        leg_id="leg-entry",
        side=Side.BUY,
        quantity_contracts=75,
        average_entry_price=f.price("100.00"),
    )
    position = f.position_state(
        trade_id=trade_id,
        intent_id="INT-LEGACY-1",
        state=TradeState.CLOSED,
        legs=(entry_leg,),
        entry_legs=(entry_leg,),
        exit_policy=f.exit_policy(trade_id=trade_id),
        opened_at=NOW,
        as_of=NOW,
    )
    lifecycle = PositionLifecycleRecord(
        trade_id=trade_id,
        position=position,
        intent=intent,
        risk_decision=decision,
        holding_style=HoldingStyle.POSITIONAL,
        as_of=NOW,
    )
    entry_order = f.order_event(
        event_id="EVT-ENTRY",
        state=OrderState.FILLED,
        filled_quantity=75,
        average_fill_price=f.price("100.00"),
        identity=f.order_identity(
            trade_id=trade_id,
            idempotency_key="IDEM-ENTRY",
            internal_order_id="ORD-ENTRY",
        ),
        command=f.order_command(side=Side.BUY, quantity_contracts=75),
    )
    exit_order = f.order_event(
        event_id="EVT-EXIT",
        state=OrderState.FILLED,
        filled_quantity=75,
        average_fill_price=f.price("80.00"),
        identity=f.order_identity(
            trade_id=trade_id,
            idempotency_key="IDEM-EXIT",
            internal_order_id="ORD-EXIT",
        ),
        command=f.order_command(side=Side.SELL, quantity_contracts=75),
    )
    gross = (Decimal("80") - Decimal("100")) * Decimal(75)  # -1500
    zero = Money.zero(Currency.INR)
    campaign = CampaignRecord(
        campaign_id=campaign_id,
        mode_id=ModeId.M3_TACTICAL_POSITIONAL,
        trade_ids=(trade_id,),
        recorded_closes=(trade_id,),
        cumulative_realized_gross=Money.of(str(gross), Currency.INR),
        cumulative_charges=zero,
        cumulative_realized_net=Money.of(str(gross), Currency.INR),
        cumulative_estimated_net=Money.of(str(gross), Currency.INR),
        high_water_mark_net=Money.of(str(gross), Currency.INR),
        drawdown=zero,
        loss_limit=Money.of("35000", Currency.INR),
    )
    reservation = f.capital_reservation(
        reservation_id=reservation_id,
        state=ReservationState.RESERVED,
        mode_id=ModeId.M3_TACTICAL_POSITIONAL,
    )
    _insert_event(connection, TradingEventType.ORDER_EVENT, entry_order, "EVT-ENTRY")
    _insert_event(connection, TradingEventType.ORDER_EVENT, exit_order, "EVT-EXIT")
    connection.execute(
        "INSERT INTO position_lifecycle (trade_id, state, payload, updated_at) "
        "VALUES (?, ?, ?, ?)",
        (
            trade_id,
            TradeState.CLOSED.value,
            lifecycle.model_dump_json(),
            NOW.isoformat(),
        ),
    )
    connection.execute(
        "INSERT INTO reservations (reservation_id, state, payload, updated_at) "
        "VALUES (?, ?, ?, ?)",
        (
            reservation_id,
            ReservationState.RESERVED.value,
            reservation.model_dump_json(),
            NOW.isoformat(),
        ),
    )
    connection.execute(
        "INSERT INTO campaign_ledger (campaign_id, mode_id, payload, updated_at) "
        "VALUES (?, ?, ?, ?)",
        (
            campaign_id,
            ModeId.M3_TACTICAL_POSITIONAL.value,
            campaign.model_dump_json(),
            NOW.isoformat(),
        ),
    )
    connection.commit()
    connection.close()
    counts = capture_row_counts(sqlite3.connect(path))
    return LegacyTradingSnapshot(
        row_counts=counts,
        trade_id=trade_id,
        gross_amount=gross,
        campaign_id=campaign_id,
        reservation_id=reservation_id,
    )


def _insert_event(
    connection: sqlite3.Connection,
    event_type: TradingEventType,
    payload: BaseModel,
    event_id: str,
) -> None:
    connection.execute(
        "INSERT INTO trading_events "
        "(event_id, event_type, payload, idempotency_key, recorded_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            event_id,
            event_type.value,
            payload.model_dump_json(),
            getattr(getattr(payload, "identity", None), "idempotency_key", None),
            NOW.isoformat(),
        ),
    )
