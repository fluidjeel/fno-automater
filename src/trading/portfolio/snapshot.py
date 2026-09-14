"""Build broker-confirmed PortfolioSnapshot instances.

Invariant 5: broker-reported funds, positions and orders are external truth.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from trading.broker.ports import BrokerFunds, BrokerPort
from trading.domain.contracts.common import ExposureSnapshot, Versions
from trading.domain.contracts.portfolio import (
    PortfolioSnapshot,
    PositionRecord,
    UnderlyingExposure,
)
from trading.domain.contracts.reservation import CapitalReservation
from trading.domain.ids import IdFactory
from trading.domain.primitives import Currency, Money

__all__ = ["build_broker_snapshot", "sum_active_reservations"]


def sum_active_reservations(
    reservations: tuple[CapitalReservation, ...],
    *,
    currency: Currency,
) -> Money:
    """Aggregate capital held by active reservations."""
    total = Money.zero(currency)
    for reservation in reservations:
        if reservation.state.holds_capital:
            total = total + reservation.amount
    return total


def build_broker_snapshot(
    broker: BrokerPort,
    *,
    account_id: str,
    versions: Versions,
    id_factory: IdFactory,
    reserved_capital: Money,
    reconciliation_ref: str | None = None,
) -> PortfolioSnapshot:
    """Query broker truth and assemble a portfolio snapshot."""
    funds = broker.get_funds()
    if funds.account_id != account_id:
        raise ValueError(
            f"broker account {funds.account_id} does not match {account_id}"
        )
    positions = broker.get_positions()
    pending_orders = broker.get_pending_orders()
    exposure = _exposure_from_broker(funds, positions)
    underlying_exposure = _underlying_exposure_from_positions(positions)
    return PortfolioSnapshot(
        portfolio_snapshot_id=id_factory.new_id("PORT"),
        account_id=account_id,
        as_of=funds.as_of,
        exposure=exposure,
        positions=positions,
        pending_orders=pending_orders,
        reserved_capital=reserved_capital,
        underlying_exposure=underlying_exposure,
        reconciliation_ref=reconciliation_ref,
        versions=versions,
    )


def _exposure_from_broker(
    funds: BrokerFunds,
    positions: tuple[PositionRecord, ...],
) -> ExposureSnapshot:
    currency = funds.equity.currency
    gross_notional = Money.zero(currency)
    unrealized_pnl = Money.zero(currency)
    net_delta = Decimal(0)
    for position in positions:
        notional = position.average_price.value * Decimal(position.quantity_contracts)
        gross_notional = gross_notional + Money.of(str(notional), currency)
        unrealized_pnl = unrealized_pnl + position.unrealized_pnl
        net_delta += Decimal(position.signed_quantity)
    return ExposureSnapshot(
        as_of=funds.as_of,
        equity=funds.equity,
        margin_used=funds.margin_used,
        margin_available=funds.margin_available,
        open_trade_count=len(positions),
        net_delta=net_delta,
        gross_notional=gross_notional,
        realized_pnl_today=Money.zero(currency),
        unrealized_pnl=unrealized_pnl,
    )


def _underlying_exposure_from_positions(
    positions: tuple[PositionRecord, ...],
) -> tuple[UnderlyingExposure, ...]:
    if not positions:
        return ()
    currency = positions[0].unrealized_pnl.currency
    by_underlying: dict[str, dict[str, Decimal | int]] = defaultdict(
        lambda: {
            "net_delta": Decimal(0),
            "gross_notional": Decimal(0),
            "open_position_count": 0,
        }
    )
    for position in positions:
        row = by_underlying[position.contract.underlying]
        row["net_delta"] = row["net_delta"] + Decimal(position.signed_quantity)
        notional = position.average_price.value * Decimal(position.quantity_contracts)
        row["gross_notional"] = row["gross_notional"] + notional
        row["open_position_count"] = row["open_position_count"] + 1
    return tuple(
        UnderlyingExposure(
            underlying=underlying,
            net_delta=int(row["net_delta"]),
            gross_notional=Money.of(str(row["gross_notional"]), currency),
            open_position_count=int(row["open_position_count"]),
        )
        for underlying, row in sorted(by_underlying.items())
    )
