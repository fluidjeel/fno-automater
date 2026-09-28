"""DISC-A21: DISCOVERY per-leg stop/target and structure pnl bounds."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import tests.factories as f
from trading.config.discovery import load_discovery_config
from trading.domain.contracts import IntentLeg, PositionLegState, TradeIntent
from trading.domain.enums import ExitScope, OptionType, Side
from trading.domain.primitives import Currency, Money
from trading.trade.discovery_exits import (
    apply_discovery_leg_exit_prices,
    build_discovery_exit_policy,
    compute_leg_stop_price,
    compute_leg_target_price,
    structure_pnl_bounds,
)
from trading.trade.exits import build_exit_policy

ROOT = Path(__file__).resolve().parent.parent
DISCOVERY = load_discovery_config(ROOT / "config" / "discovery.yaml").config
EXITS = DISCOVERY.exits
QTY = 65


def _long_leg(
    *,
    leg_id: str,
    symbol: str,
    entry: str,
    strike: str = "22800",
    option_type: OptionType = OptionType.PUT,
) -> PositionLegState:
    contract = f.option_contract(
        symbol=symbol,
        strike=Decimal(strike),
        option_type=option_type,
    )
    entry_price = f.price(entry)
    return PositionLegState(
        leg_id=leg_id,
        contract=contract,
        side=Side.BUY,
        quantity_contracts=QTY,
        average_entry_price=entry_price,
    )


def _strangle_legs() -> tuple[PositionLegState, ...]:
    return (
        _long_leg(
            leg_id="put",
            symbol="NIFTY26SEP22800PE",
            entry="137.25",
            strike="22800",
            option_type=OptionType.PUT,
        ),
        _long_leg(
            leg_id="call",
            symbol="NIFTY26SEP22900CE",
            entry="202.75",
            strike="22900",
            option_type=OptionType.CALL,
        ),
    )


def _strangle_intent(legs: tuple[PositionLegState, ...]) -> TradeIntent:
    return f.intent(
        legs=(
            IntentLeg(leg_id="put", contract=legs[0].contract, side=Side.BUY, ratio=1),
            IntentLeg(leg_id="call", contract=legs[1].contract, side=Side.BUY, ratio=1),
        ),
        estimated_max_loss=Money.of("50000", Currency.INR),
    )


class TestDiscoveryLegPrices:
    def test_strangle_per_leg_stops_and_targets(self) -> None:
        """Oracle 2026-09-28 long strangle leg prices from each entry."""
        legs = apply_discovery_leg_exit_prices(_strangle_legs(), EXITS)
        put, call = legs
        assert put.current_stop_price == f.price("89.20")
        assert put.current_target_price == f.price("247.05")
        assert call.current_stop_price == f.price("131.75")
        assert call.current_target_price == f.price("364.95")
        assert put.current_stop_price != call.current_stop_price
        assert put.current_target_price != call.current_target_price

    def test_straddle_structure_pnl_bounds(self) -> None:
        """Long straddle ~22950: CE 176.05 + PE 200.65 per unit."""
        legs = (
            _long_leg(
                leg_id="call",
                symbol="NIFTY26SEP22950CE",
                entry="176.05",
                strike="22950",
            ),
            _long_leg(
                leg_id="put",
                symbol="NIFTY26SEP22950PE",
                entry="200.65",
                strike="22950",
                option_type=OptionType.PUT,
            ),
        )
        pnl_stop, pnl_target = structure_pnl_bounds(legs, EXITS)
        assert pnl_stop == Money.of("-8569.92", Currency.INR)
        assert pnl_target == Money.of("19588.40", Currency.INR)

    def test_strangle_structure_pnl_matches_leg_fractions(self) -> None:
        legs = _strangle_legs()
        pnl_stop, pnl_target = structure_pnl_bounds(legs, EXITS)
        assert pnl_stop == Money.of("-7735.00", Currency.INR)
        assert pnl_target == Money.of("17680.00", Currency.INR)

    def test_credit_short_leg_prices_and_structure_pnl(self) -> None:
        short = PositionLegState(
            leg_id="short",
            contract=f.option_contract(symbol="NIFTY26SEP24000CE"),
            side=Side.SELL,
            quantity_contracts=QTY,
            average_entry_price=f.price("50.00"),
        )
        stop = compute_leg_stop_price(
            entry_price=short.average_entry_price, side=Side.SELL, config=EXITS
        )
        target = compute_leg_target_price(
            entry_price=short.average_entry_price, side=Side.SELL, config=EXITS
        )
        assert stop == f.price("100.00")
        assert target == f.price("25.00")
        pnl_stop, pnl_target = structure_pnl_bounds((short,), EXITS)
        assert pnl_stop == Money.of("-3250.00", Currency.INR)
        assert pnl_target == Money.of("1625.00", Currency.INR)

    def test_no_leg_copies_strategy_stop_price(self) -> None:
        legs = apply_discovery_leg_exit_prices(_strangle_legs(), EXITS)
        policy = build_discovery_exit_policy(
            _strangle_intent(legs),
            legs,
            f.exit_template(stop_distance_ticks=40),
            trade_id="TRD-NOCOPY",
            policy_id="EXIT-NOCOPY",
            initialized_at=f.NOW,
            config=EXITS,
            scope=ExitScope.STRATEGY_PNL,
        )
        assert policy.stop_price is None
        assert all(
            leg.current_stop_price != policy.stop_price
            for leg in legs
            if leg.current_stop_price
        )
        assert len({leg.current_stop_price for leg in legs}) == 2

    def test_strict_tick_policy_unchanged(self) -> None:
        """STRICT keeps 40-tick policy (Rs 130 stop on qty 65)."""
        policy = build_exit_policy(
            f.exit_template(stop_distance_ticks=40),
            trade_id="TRD-STRICT",
            policy_id="EXIT-STRICT",
            entry_price=f.price("100.00"),
            initialized_at=f.NOW,
            scope=ExitScope.STRATEGY_PNL,
            quantity_contracts=QTY,
        )
        assert policy.premium_scaled is False
        assert policy.pnl_stop == Money.of("-130.00", Currency.INR)
