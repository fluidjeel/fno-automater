"""Conservative fill reconstruction. Not a broker adapter.

Promotion evidence must not use last-trade-at-limit fantasy fills. This
calculator re-prices a recorded command against the decision-time book.
"""

from __future__ import annotations

from decimal import Decimal

from trading.config.evaluation import FillModelConfig
from trading.config.schema import ConfigNotVerifiedError
from trading.domain.contracts.evaluation import FillSimulation
from trading.domain.contracts.order import OrderCommand
from trading.domain.contracts.snapshot import MarketQuote
from trading.domain.enums import FillOutcome, ReasonCode, Side
from trading.domain.primitives import Currency, Money, Price, Rounding, TickSize

__all__ = ["simulate_fill", "simulate_legs"]

_INR = Currency.INR
FillLeg = tuple[OrderCommand, MarketQuote, bool]


def simulate_fill(
    command: OrderCommand,
    quote: MarketQuote,
    *,
    policy: FillModelConfig,
    traded_through: bool | None = None,
    charges_lots: int = 1,
) -> FillSimulation:
    """Reconstruct one fill from the decision-time book and policy version."""
    if policy.version == "touch-v1":
        return _simulate_touch_fill(
            command,
            quote,
            policy=policy,
            charges_lots=charges_lots,
        )
    return _simulate_conservative_fill(
        command,
        quote,
        policy=policy,
        traded_through=traded_through,
        charges_lots=charges_lots,
    )


def _simulate_conservative_fill(
    command: OrderCommand,
    quote: MarketQuote,
    *,
    policy: FillModelConfig,
    traded_through: bool | None = None,
    charges_lots: int = 1,
) -> FillSimulation:
    """Reconstruct one conservative fill from the decision-time book."""
    intended = command.limit_price
    if intended is None:
        return FillSimulation(
            outcome=FillOutcome.REJECTED,
            filled_quantity=0,
            intended_price=_quote_anchor(quote, command.side),
            reason_code=ReasonCode.PRICE_UNAVAILABLE,
            reason_detail="conservative fill requires a LIMIT command",
        )

    book_price = quote.ask if command.side is Side.BUY else quote.bid
    depth = quote.ask_size if command.side is Side.BUY else quote.bid_size
    if book_price is None:
        return FillSimulation(
            outcome=FillOutcome.REJECTED,
            filled_quantity=0,
            intended_price=intended,
            reason_code=ReasonCode.PRICE_UNAVAILABLE,
            reason_detail="missing bid/ask for conservative fill",
        )

    conservative = _conservative_price(command.side, book_price, policy.slippage_ticks)
    if conservative is None:
        return FillSimulation(
            outcome=FillOutcome.REJECTED,
            filled_quantity=0,
            intended_price=intended,
            reason_code=ReasonCode.PRICE_UNAVAILABLE,
            reason_detail="conservative price is off the tick grid or negative",
        )

    through = (
        _limit_traded_through(command.side, intended, quote)
        if traded_through is None
        else traded_through
    )
    if policy.require_traded_through and not through:
        return FillSimulation(
            outcome=FillOutcome.UNFILLED,
            filled_quantity=0,
            intended_price=intended,
            reason_code=ReasonCode.SLIPPAGE_EXCEEDED,
            reason_detail="market did not trade through the limit",
        )

    if depth is None or depth < command.quantity_contracts:
        return FillSimulation(
            outcome=FillOutcome.UNFILLED,
            filled_quantity=0,
            intended_price=intended,
            reason_code=ReasonCode.DEPTH_INSUFFICIENT,
            reason_detail="displayed depth is below requested quantity",
        )

    slippage = _slippage_money(command.side, intended, conservative)
    charges, confirmed = _charges(policy, lots=charges_lots)
    return FillSimulation(
        outcome=FillOutcome.FILLED,
        filled_quantity=command.quantity_contracts,
        intended_price=intended,
        fill_price=conservative,
        slippage=slippage,
        charges=charges,
        charges_confirmed=confirmed,
        reason_code=ReasonCode.OK,
    )


def _simulate_touch_fill(
    command: OrderCommand,
    quote: MarketQuote,
    *,
    policy: FillModelConfig,
    charges_lots: int = 1,
) -> FillSimulation:
    """DISCOVERY touch fill: BUY at ask, SELL at bid, plus charges."""
    intended = command.limit_price
    if intended is None:
        return FillSimulation(
            outcome=FillOutcome.REJECTED,
            filled_quantity=0,
            intended_price=_quote_anchor(quote, command.side),
            reason_code=ReasonCode.PRICE_UNAVAILABLE,
            reason_detail="touch fill requires a LIMIT command",
        )

    book_price = quote.ask if command.side is Side.BUY else quote.bid
    if book_price is None:
        return FillSimulation(
            outcome=FillOutcome.REJECTED,
            filled_quantity=0,
            intended_price=intended,
            reason_code=ReasonCode.PRICE_UNAVAILABLE,
            reason_detail="missing bid/ask for touch fill",
        )

    slippage = _slippage_money(command.side, intended, book_price)
    charges, confirmed = _charges(policy, lots=charges_lots)
    return FillSimulation(
        outcome=FillOutcome.FILLED,
        filled_quantity=command.quantity_contracts,
        intended_price=intended,
        fill_price=book_price,
        slippage=slippage,
        charges=charges,
        charges_confirmed=confirmed,
        reason_code=ReasonCode.OK,
    )


def simulate_legs(
    legs: tuple[FillLeg, ...],
    *,
    policy: FillModelConfig,
    charges_lots: int = 1,
) -> tuple[FillSimulation, ...]:
    """Fill legs sequentially; later books move adversely by legging delay."""
    results: list[FillSimulation] = []
    for index, (command, quote, traded_through) in enumerate(legs):
        shifted = quote
        if index > 0 and policy.legging_delay_ticks > 0:
            shifted = _adverse_quote(quote, command.side, policy.legging_delay_ticks)
        results.append(
            simulate_fill(
                command,
                shifted,
                policy=policy,
                traded_through=traded_through,
                charges_lots=charges_lots,
            )
        )
    return tuple(results)


def _conservative_price(side: Side, book: Price, slippage_ticks: int) -> Price | None:
    tick = book.tick
    delta = tick.value * slippage_ticks
    raw = book.value + delta if side is Side.BUY else book.value - delta
    if raw < 0:
        return None
    rounding = Rounding.CEILING if side is Side.BUY else Rounding.FLOOR
    return Price.snap(raw, tick, rounding=rounding)


def _limit_traded_through(side: Side, limit: Price, quote: MarketQuote) -> bool:
    last = quote.last
    if last is None:
        return False
    if side is Side.BUY:
        return last <= limit
    return last >= limit


def _quote_anchor(quote: MarketQuote, side: Side) -> Price:
    fallback = quote.ask if side is Side.BUY else quote.bid
    if fallback is not None:
        return fallback
    if quote.last is not None:
        return quote.last
    return Price.snap(0, TickSize.of("0.05"))


def _slippage_money(side: Side, intended: Price, fill: Price) -> Money:
    if side is Side.BUY:
        delta = fill.value - intended.value
    else:
        delta = intended.value - fill.value
    if delta < 0:
        delta = Decimal(0)
    return Money.of(str(delta), _INR)


def _adverse_quote(quote: MarketQuote, side: Side, ticks: int) -> MarketQuote:
    anchor = quote.ask or quote.bid or quote.last
    if anchor is None:
        return quote
    grid = anchor.tick
    shift = grid.value * ticks
    bid = quote.bid
    ask = quote.ask
    if side is Side.BUY and ask is not None:
        ask = Price.snap(ask.value + shift, grid, rounding=Rounding.CEILING)
    if side is Side.SELL and bid is not None:
        raw = bid.value - shift
        if raw >= 0:
            bid = Price.snap(raw, grid, rounding=Rounding.FLOOR)
    return quote.model_copy(update={"bid": bid, "ask": ask})


def _charges(policy: FillModelConfig, *, lots: int) -> tuple[Money | None, bool]:
    try:
        per_lot = policy.charges_per_lot.require("fill_model.charges_per_lot")
    except ConfigNotVerifiedError:
        return None, False
    return Money.of(str(per_lot * lots), _INR), True
