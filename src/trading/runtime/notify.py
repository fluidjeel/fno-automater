"""Advisory Telegram copy for the paper session. No promotion language."""

from __future__ import annotations

from collections.abc import Sequence

from trading.domain.contracts.order import OrderEvent
from trading.domain.enums import OrderState, RiskAction
from trading.runtime.paper_runner import (
    LifecycleAlert,
    PaperCycleResult,
    PaperStrategyOutcome,
)

__all__ = ["format_eod_report", "format_lifecycle_alert", "format_post_trade"]


def format_post_trade(
    outcome: PaperStrategyOutcome,
    *,
    experiment_id: str,
) -> tuple[str, ...]:
    """One advisory line per fill or post-intent reject."""
    messages: list[str] = []
    for event in outcome.order_events:
        messages.append(_order_line(outcome.strategy_id, experiment_id, event))
    if not outcome.order_events:
        for decision in outcome.decisions:
            if decision.action is RiskAction.REJECT:
                codes = decision.reason_codes
                reason = ",".join(code.value for code in codes) or "REJECT"
                messages.append(
                    f"PAPER reject {outcome.strategy_id} {reason} "
                    f"experiment={experiment_id}"
                )
    return tuple(messages)


def format_eod_report(
    results: Sequence[PaperCycleResult],
    *,
    open_trade_count: int,
    charges_verified: bool,
) -> str:
    """Session summary. Net expectancy stays unknown until charges are verified."""
    by_strategy: dict[str, list[int]] = {}
    for result in results:
        for outcome in result.outcomes:
            counts = by_strategy.setdefault(outcome.strategy_id, [0, 0, 0])
            counts[0] += len(outcome.intents)
            counts[1] += sum(
                1 for event in outcome.order_events if event.state is OrderState.FILLED
            )
            counts[2] += _reject_count(outcome)
    lines = ["PAPER EOD (advisory, not a promotion record)"]
    for strategy_id, (signals, fills, rejects) in sorted(by_strategy.items()):
        lines.append(
            f"{strategy_id}: signals={signals} fills={fills} rejects={rejects}"
        )
    lines.append(f"open_positions={open_trade_count}")
    if charges_verified:
        lines.append("charges_per_lot is verified; net P&L may be scored.")
    else:
        lines.append("net expectancy is unknown: charges_per_lot.verified_at is unset.")
    return "\n".join(lines)


def format_lifecycle_alert(alert: LifecycleAlert) -> str:
    """Advisory recovery alert. Not a promotion or flatten instruction."""
    return (
        f"PAPER recovery {alert.reason_code.value} trade={alert.trade_id} "
        f"{alert.detail}"
    )


def _order_line(strategy_id: str, experiment_id: str, event: OrderEvent) -> str:
    symbol = event.command.contract.symbol
    side = event.command.side.value
    if event.state is OrderState.FILLED:
        price = event.average_fill_price
        detail = f"fill {price.value if price is not None else '?'}"
    else:
        detail = f"{event.state.value} {event.reason_code or event.reason_detail}"
    return f"PAPER {detail} {strategy_id} {symbol} {side} experiment={experiment_id}"


def _reject_count(outcome: PaperStrategyOutcome) -> int:
    rejects = sum(
        1 for event in outcome.order_events if event.state is OrderState.REJECTED
    )
    rejects += sum(
        1 for decision in outcome.decisions if decision.action is RiskAction.REJECT
    )
    rejects += len(outcome.rejection_reasons)
    return rejects
