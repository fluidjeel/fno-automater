"""Conservative fill reconstruction (L4-002) and paper-path adoption (PAPER-002)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import tests.factories as f
from trading.analytics.fills import simulate_fill, simulate_legs
from trading.config.evaluation import FillModelConfig, load_evaluation_config
from trading.config.schema import VerifiedValue
from trading.domain.enums import FillOutcome, OrderType, ReasonCode, Side
from trading.domain.primitives import Price

ROOT = Path(__file__).resolve().parent.parent
EVALUATION = ROOT / "config" / "evaluation.yaml"


def shipped_fill_model() -> FillModelConfig:
    return load_evaluation_config(EVALUATION).config.fill_model


def verified_fill_model() -> FillModelConfig:
    return FillModelConfig(
        version="conservative-v1",
        slippage_ticks=1,
        legging_delay_ticks=2,
        require_traded_through=True,
        charges_per_lot=VerifiedValue(
            value=Decimal("10"),
            source="test fixture contract note",
            verified_at=date(2026, 9, 14),
        ),
    )


class TestConservativeFillCalculator:
    def test_long_entry_fills_at_ask_plus_slippage(self) -> None:
        quote = f.quote(bid_size=300, ask_size=300, last=f.price("100.05"))
        command = f.order_command(
            side=Side.BUY, quantity_contracts=75, limit_price=f.price("100.05")
        )
        fill = simulate_fill(command, quote, policy=verified_fill_model())
        assert fill.outcome is FillOutcome.FILLED
        assert fill.fill_price == Price.snap("100.10", f.TICK)
        assert fill.charges_confirmed is True
        assert fill.charges == f.money("10")

    def test_long_exit_fills_at_bid_minus_slippage(self) -> None:
        quote = f.quote(bid_size=300, ask_size=300, last=f.price("100.00"))
        command = f.order_command(
            side=Side.SELL, quantity_contracts=75, limit_price=f.price("100.00")
        )
        fill = simulate_fill(command, quote, policy=verified_fill_model())
        assert fill.outcome is FillOutcome.FILLED
        assert fill.fill_price == Price.snap("99.95", f.TICK)

    def test_short_entry_fills_at_bid_minus_slippage(self) -> None:
        quote = f.quote(bid_size=300, ask_size=300, last=f.price("100.00"))
        command = f.order_command(
            side=Side.SELL, quantity_contracts=75, limit_price=f.price("100.00")
        )
        fill = simulate_fill(command, quote, policy=verified_fill_model())
        assert fill.fill_price == Price.snap("99.95", f.TICK)

    def test_limit_without_trade_through_is_unfilled(self) -> None:
        quote = f.quote(
            bid=f.price("91.95"),
            ask=f.price("92.00"),
            last=f.price("93.00"),
            bid_size=300,
            ask_size=300,
        )
        command = f.order_command(
            side=Side.BUY, quantity_contracts=75, limit_price=f.price("92.00")
        )
        fill = simulate_fill(command, quote, policy=shipped_fill_model())
        assert fill.outcome is FillOutcome.UNFILLED
        assert fill.reason_code is ReasonCode.SLIPPAGE_EXCEEDED
        assert fill.fill_price is None

    def test_depth_below_quantity_is_unfilled(self) -> None:
        quote = f.quote(bid_size=10, ask_size=10, last=f.price("100.05"))
        command = f.order_command(
            side=Side.BUY, quantity_contracts=75, limit_price=f.price("100.05")
        )
        fill = simulate_fill(command, quote, policy=shipped_fill_model())
        assert fill.outcome is FillOutcome.UNFILLED
        assert fill.reason_code is ReasonCode.DEPTH_INSUFFICIENT

    def test_market_order_is_rejected(self) -> None:
        command = f.order_command(
            order_type=OrderType.MARKET, limit_price=None, quantity_contracts=75
        )
        fill = simulate_fill(command, f.quote(), policy=shipped_fill_model())
        assert fill.outcome is FillOutcome.REJECTED
        assert fill.reason_code is ReasonCode.PRICE_UNAVAILABLE

    def test_missing_book_is_rejected(self) -> None:
        quote = f.quote(ask=None, last=f.price("100.00"))
        command = f.order_command(side=Side.BUY, limit_price=f.price("100.05"))
        fill = simulate_fill(command, quote, policy=shipped_fill_model())
        assert fill.outcome is FillOutcome.REJECTED
        assert fill.reason_code is ReasonCode.PRICE_UNAVAILABLE

    def test_unverified_charges_fail_closed(self) -> None:
        quote = f.quote(bid_size=300, ask_size=300, last=f.price("100.05"))
        command = f.order_command(
            side=Side.BUY, quantity_contracts=75, limit_price=f.price("100.05")
        )
        unverified = shipped_fill_model().model_copy(
            update={
                "charges_per_lot": VerifiedValue(
                    value=None,
                    source="test deliberately unverified",
                    verified_at=None,
                    note="fail-closed path",
                )
            }
        )
        fill = simulate_fill(command, quote, policy=unverified)
        assert fill.outcome is FillOutcome.FILLED
        assert fill.charges_confirmed is False
        assert fill.charges is None

    def test_later_legs_move_adversely(self) -> None:
        first = f.order_command(
            side=Side.BUY, quantity_contracts=75, limit_price=f.price("100.05")
        )
        second = f.order_command(
            side=Side.BUY,
            quantity_contracts=75,
            limit_price=f.price("100.20"),
        )
        quote = f.quote(bid_size=300, ask_size=300, last=f.price("100.05"))
        later = f.quote(
            bid_size=300,
            ask_size=300,
            last=f.price("100.20"),
            ask=f.price("100.05"),
            bid=f.price("100.00"),
        )
        fills = simulate_legs(
            ((first, quote, True), (second, later, True)),
            policy=verified_fill_model(),
        )
        assert fills[0].fill_price == Price.snap("100.10", f.TICK)
        # Second leg is shifted +2 ticks from ask 100.05 -> 100.15, then +1 slip.
        assert fills[1].fill_price == Price.snap("100.20", f.TICK)
