"""Deterministic reason_text templates for DISCOVERY_DECISION records."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

from trading.domain.enums import DiscoveryDecisionKind, ReasonCode

__all__ = ["DecisionTextContext", "render_decision_text", "render_exit_text"]


@dataclass(frozen=True, slots=True)
class DecisionTextContext:
    """Optional numeric and string inputs for template rendering."""

    return_15m: Decimal | None = None
    return_60m: Decimal | None = None
    trend_score: Decimal | None = None
    threshold: Decimal | None = None
    conviction_score: Decimal | None = None
    detail: str | None = None
    mode_id: str | None = None
    family_id: str | None = None
    symbol: str | None = None
    exit_rule: str | None = None
    exit_price: str | None = None
    realized_pnl: str | None = None


def _pct(value: Decimal | None) -> str:
    if value is None:
        return "n/a"
    return f"{value * Decimal('100'):+.2f}%"


def _num(value: Decimal | None, *, places: int = 2) -> str:
    if value is None:
        return "n/a"
    fmt = f"{{:.{places}f}}"
    return fmt.format(value)


def render_decision_text(
    *,
    decision: DiscoveryDecisionKind,
    reason_codes: tuple[ReasonCode, ...],
    ctx: DecisionTextContext | None = None,
) -> str:
    """Build one deterministic sentence from reason codes and context."""
    ctx = ctx or DecisionTextContext()
    primary = reason_codes[0] if reason_codes else ReasonCode.OK
    if decision is DiscoveryDecisionKind.TRADE:
        return f"Trade approved: {primary.value}."
    template = _TEMPLATES.get(primary, _DEFAULT_TEMPLATE)
    return template(ctx, reason_codes)


def render_exit_text(
    *,
    rule: str,
    ctx: DecisionTextContext | None = None,
) -> str:
    """Build deterministic exit narrative."""
    ctx = ctx or DecisionTextContext()
    price = ctx.exit_price or "n/a"
    pnl = ctx.realized_pnl or "n/a"
    return f"Exit {rule}: price {price}, P&L {pnl}."


def _direction_neutral(ctx: DecisionTextContext, _codes: tuple[ReasonCode, ...]) -> str:
    threshold = _num(ctx.threshold, places=2)
    return (
        "No trade: direction neutral "
        f"(15m {_pct(ctx.return_15m)}, 60m {_pct(ctx.return_60m)}, "
        f"score {_num(ctx.trend_score)} below {threshold})."
    )


def _option_type_mismatch(
    ctx: DecisionTextContext, _codes: tuple[ReasonCode, ...]
) -> str:
    detail = ctx.detail or "candidate legs do not match directional read"
    return f"No trade: option type mismatch ({detail})."


def _microstructure_unconfirmed(
    ctx: DecisionTextContext, _codes: tuple[ReasonCode, ...]
) -> str:
    detail = ctx.detail or "CAS microstructure did not confirm direction"
    return f"No trade: microstructure unconfirmed ({detail})."


def _conviction_below(ctx: DecisionTextContext, _codes: tuple[ReasonCode, ...]) -> str:
    score = _num(ctx.conviction_score, places=0)
    threshold = _num(ctx.threshold, places=0)
    return f"No trade: conviction score {score} below threshold {threshold}."


def _regime_not_range(ctx: DecisionTextContext, _codes: tuple[ReasonCode, ...]) -> str:
    detail = ctx.detail or "market regime is not range-bound"
    return f"No trade: regime not range ({detail})."


def _vol_compressed(ctx: DecisionTextContext, _codes: tuple[ReasonCode, ...]) -> str:
    detail = ctx.detail or "realized volatility compressed vs implied"
    return f"No trade: volatility compressed ({detail})."


def _generic(ctx: DecisionTextContext, codes: tuple[ReasonCode, ...]) -> str:
    detail = ctx.detail or ", ".join(code.value for code in codes)
    return f"No trade: {detail}."


def _blocked(ctx: DecisionTextContext, codes: tuple[ReasonCode, ...]) -> str:
    detail = ctx.detail or ", ".join(code.value for code in codes)
    return f"Blocked: {detail}."


_DEFAULT_TEMPLATE: Callable[[DecisionTextContext, tuple[ReasonCode, ...]], str] = (
    _generic
)

_TEMPLATES: dict[
    ReasonCode, Callable[[DecisionTextContext, tuple[ReasonCode, ...]], str]
] = {
    ReasonCode.DIRECTION_NEUTRAL: _direction_neutral,
    ReasonCode.OPTION_TYPE_MISMATCH: _option_type_mismatch,
    ReasonCode.MICROSTRUCTURE_UNCONFIRMED: _microstructure_unconfirmed,
    ReasonCode.CONVICTION_BELOW_THRESHOLD: _conviction_below,
    ReasonCode.REGIME_NOT_RANGE: _regime_not_range,
    ReasonCode.VOL_COMPRESSED: _vol_compressed,
    ReasonCode.ENTRY_FROZEN: _blocked,
    ReasonCode.DATA_STALE: _blocked,
    ReasonCode.DATA_INVALID: _blocked,
    ReasonCode.PRICE_UNAVAILABLE: _blocked,
    ReasonCode.RISK_LIMIT_TRADE: _blocked,
    ReasonCode.EXACT_DUPLICATE_SUPPRESSED: _generic,
    ReasonCode.ECONOMIC_OVERLAP_SUPPRESSED: _generic,
    ReasonCode.DAILY_ENTRY_CAP: _blocked,
    ReasonCode.M4_POSITION_CAP_REACHED: _blocked,
    ReasonCode.INSTRUMENT_UNKNOWN: _generic,
    ReasonCode.DEPTH_INSUFFICIENT: _generic,
    ReasonCode.SPREAD_TOO_WIDE: _generic,
    ReasonCode.DIRECTION_UNRESOLVED: _direction_neutral,
}
