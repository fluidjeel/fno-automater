"""Pre-trade limit evaluation and sizing limit construction."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal

from trading.config.discovery import DiscoveryConfig
from trading.config.risk_policy import RiskPolicyConfig
from trading.config.schema import RiskLimits
from trading.domain.contracts.common import ExposureSnapshot
from trading.domain.contracts.exposure import ExposureReport
from trading.domain.contracts.intent import TradeIntent
from trading.domain.contracts.mode_policy import ModesConfig, load_modes_config
from trading.domain.contracts.portfolio import PortfolioSnapshot, UnderlyingExposure
from trading.domain.contracts.sizing import SizingLimits
from trading.domain.enums import ModeId, ReasonCode, Side
from trading.domain.primitives import Money, Rounding
from trading.portfolio.campaign_drawdown import CampaignLedger
from trading.risk.mode_ledger import FourModeBook, ModeLedger

__all__ = [
    "LimitEvaluation",
    "build_sizing_limits",
    "evaluate_campaign_limit",
    "evaluate_exposure_limits",
    "evaluate_pre_trade_limits",
    "floor_divide_money",
    "open_trade_slots",
    "premium_budget_used",
    "project_post_trade_exposure",
]


@dataclass(frozen=True, slots=True)
class LimitEvaluation:
    """Outcome of a pre-trade limit check."""

    passed: bool
    reason_codes: tuple[ReasonCode, ...]
    applied_limits: tuple[str, ...]


def floor_divide_money(numerator: Money, divisor: Money) -> int:
    """Return whole lots affordable without exceeding the numerator budget."""
    if divisor.is_zero or divisor.is_negative or numerator.is_negative:
        return 0
    if numerator.is_zero:
        return 0
    ratio = numerator.amount / divisor.amount
    return max(int(ratio.to_integral_value(rounding=ROUND_FLOOR)), 0)


def build_sizing_limits(
    portfolio: PortfolioSnapshot,
    account_risk: RiskLimits,
    policy: RiskPolicyConfig,
    strategy_id: str,
    *,
    config_version: str,
    mode_id: ModeId | None = None,
    modes_config: ModesConfig | None = None,
    mode_ledger: ModeLedger | None = None,
    discovery_config: DiscoveryConfig | None = None,
) -> SizingLimits:
    """Derive the limit snapshot used by the sizing engine."""
    if mode_id is not None or mode_ledger is not None:
        effective_mode_id = mode_id or (
            mode_ledger.mode_id if mode_ledger is not None else None
        )
        if effective_mode_id is None:
            raise ValueError("mode_id or mode_ledger must be provided")
        cfg = modes_config or load_modes_config()
        mode_policy = cfg.modes[effective_mode_id]
        if mode_ledger is None:
            if discovery_config is not None:
                alloc = Money.of(
                    str(discovery_config.books.starting_equity_per_mode),
                    portfolio.exposure.equity.currency,
                )
            else:
                share = mode_policy.capital_share
                alloc = (portfolio.exposure.equity * share).quantized(Rounding.FLOOR)
            mode_ledger = ModeLedger(
                mode_id=effective_mode_id,
                allocated_capital=alloc,
                reserved_capital=portfolio.reserved_capital,
                realized_pnl_today=portfolio.exposure.realized_pnl_today,
                unrealized_pnl=portfolio.exposure.unrealized_pnl,
                margin_used=portfolio.exposure.margin_used,
            )
        max_loss_per_trade = (
            mode_ledger.reference_capital * mode_policy.per_trade_loss_cap_fraction
        ).quantized(Rounding.FLOOR)
        daily_loss_remaining = mode_ledger.daily_loss_remaining(
            mode_policy.daily_budget_cap_fraction
        )
        strategy_allocation_remaining = mode_ledger.available_capital
        margin_available = mode_ledger.available_capital
        return SizingLimits(
            policy_version=policy.policy_version,
            config_version=config_version,
            max_loss_per_trade=max_loss_per_trade,
            daily_loss_remaining=daily_loss_remaining,
            strategy_allocation_remaining=strategy_allocation_remaining,
            margin_available=margin_available,
        )

    allocation = policy.allocation_for(strategy_id)
    equity = portfolio.exposure.equity
    currency = equity.currency
    max_loss_per_trade = (equity * account_risk.max_loss_per_trade_fraction).quantized(
        Rounding.FLOOR
    )
    daily_cap = (equity * account_risk.daily_loss_cap_fraction).quantized(
        Rounding.FLOOR
    )
    # Conservative net (min of confirmed and estimated charges) so understated
    # fees cannot mask a daily-loss breach.
    realized = portfolio.exposure.realized_pnl_today
    daily_loss_remaining = (daily_cap + realized).quantized(Rounding.FLOOR)
    if daily_loss_remaining.is_negative:
        daily_loss_remaining = Money.zero(currency)
    strategy_budget = (equity * allocation.allocation_fraction).quantized(
        Rounding.FLOOR
    )
    strategy_allocation_remaining = (
        strategy_budget - portfolio.reserved_capital
    ).quantized(Rounding.FLOOR)
    if strategy_allocation_remaining.is_negative:
        strategy_allocation_remaining = Money.zero(currency)
    margin_available = portfolio.available_after_reservations
    return SizingLimits(
        policy_version=policy.policy_version,
        config_version=config_version,
        max_loss_per_trade=max_loss_per_trade,
        daily_loss_remaining=daily_loss_remaining,
        strategy_allocation_remaining=strategy_allocation_remaining,
        margin_available=margin_available,
    )


def premium_budget_used(portfolio: PortfolioSnapshot) -> Money:
    """Approximate long-option premium tied up in open positions."""
    currency = portfolio.exposure.equity.currency
    total = Money.zero(currency)
    for position in portfolio.positions:
        if position.side is not Side.BUY:
            continue
        notional = position.average_price.value * Decimal(position.quantity_contracts)
        total = total + Money.of(str(notional), currency)
    return total.quantized(Rounding.FLOOR)


def underlying_exposure_for(
    portfolio: PortfolioSnapshot,
    underlying: str,
) -> UnderlyingExposure | None:
    for row in portfolio.underlying_exposure:
        if row.underlying == underlying:
            return row
    return None


def open_trade_slots(portfolio: PortfolioSnapshot, account_risk: RiskLimits) -> int:
    """Remaining concurrent trade slots before the account cap binds."""
    working = len(portfolio.positions) + len(portfolio.pending_orders)
    return max(account_risk.max_concurrent_trades - working, 0)


def evaluate_campaign_limit(
    campaign_id: str | None,
    campaign_ledger: CampaignLedger | None,
    *,
    recalculated_max_loss: Money,
) -> LimitEvaluation:
    """Fail closed when a roll campaign has breached or would breach its loss cap.

    Uses conservative campaign net (min of broker-confirmed and model-estimated).
    """
    if campaign_id is None or campaign_ledger is None:
        return LimitEvaluation(
            passed=True,
            reason_codes=(ReasonCode.OK,),
            applied_limits=(),
        )
    record = campaign_ledger.get(campaign_id)
    if record is None:
        return LimitEvaluation(
            passed=True,
            reason_codes=(ReasonCode.OK,),
            applied_limits=(),
        )
    if record.entries_blocked or campaign_ledger.projected_loss_limit_breached(
        campaign_id=campaign_id,
        additional_risk=recalculated_max_loss,
    ):
        return LimitEvaluation(
            passed=False,
            reason_codes=(ReasonCode.CAMPAIGN_LOSS_LIMIT,),
            applied_limits=("campaign_loss_limit",),
        )
    return LimitEvaluation(
        passed=True,
        reason_codes=(ReasonCode.OK,),
        applied_limits=(),
    )


def evaluate_pre_trade_limits(
    intent: TradeIntent,
    portfolio: PortfolioSnapshot,
    account_risk: RiskLimits,
    policy: RiskPolicyConfig,
    limits: SizingLimits,
    *,
    recalculated_max_loss: Money,
    approved_lots: int,
    mode_ledger: ModeLedger | None = None,
    modes_config: ModesConfig | None = None,
    mode_book: FourModeBook | None = None,
) -> LimitEvaluation:
    """Fail closed when a hard limit would be breached by the proposed trade."""
    if approved_lots <= 0:
        return LimitEvaluation(
            passed=False,
            reason_codes=(ReasonCode.SIZE_BELOW_MINIMUM,),
            applied_limits=("minimum_lot",),
        )
    reasons: list[ReasonCode] = []
    applied: list[str] = []
    if recalculated_max_loss > limits.max_loss_per_trade:
        trade_loss_code = (
            ReasonCode.MIN_LOT_EXCEEDS_BUDGET
            if intent.mode_id is not None
            else ReasonCode.RISK_LIMIT_TRADE
        )
        reasons.append(trade_loss_code)
        applied.append("max_loss_per_trade")
    if recalculated_max_loss > limits.daily_loss_remaining:
        reasons.append(ReasonCode.RISK_LIMIT_DAILY_LOSS)
        applied.append("daily_loss_remaining")
    if recalculated_max_loss > limits.strategy_allocation_remaining:
        reasons.append(ReasonCode.RISK_LIMIT_STRATEGY)
        applied.append("strategy_allocation_remaining")
    equity = portfolio.exposure.equity
    premium_budget = (equity * policy.options_premium_budget_fraction).quantized(
        Rounding.FLOOR
    )
    premium_remaining = premium_budget - premium_budget_used(portfolio)
    if recalculated_max_loss > premium_remaining:
        reasons.append(ReasonCode.RISK_LIMIT_PORTFOLIO)
        applied.append("options_premium_budget")
    if open_trade_slots(portfolio, account_risk) <= 0:
        reasons.append(ReasonCode.RISK_LIMIT_PORTFOLIO)
        applied.append("max_concurrent_trades")
    concentration_cap = (equity * policy.underlying_concentration_fraction).quantized(
        Rounding.FLOOR
    )
    current = underlying_exposure_for(portfolio, intent.underlying)
    current_notional = (
        current.gross_notional if current is not None else Money.zero(equity.currency)
    )
    if current_notional + recalculated_max_loss > concentration_cap:
        reasons.append(ReasonCode.CONCENTRATION_LIMIT)
        applied.append("underlying_concentration")

    if (
        intent.mode_id is not None
        and mode_ledger is not None
        and modes_config is not None
    ):
        mode_policy = modes_config.modes[intent.mode_id]
        mode_open_cap = (
            mode_ledger.reference_capital * mode_policy.max_open_loss_cap_fraction
        ).quantized(Rounding.FLOOR)
        if mode_ledger.open_risk + recalculated_max_loss > mode_open_cap:
            reasons.append(ReasonCode.RISK_LIMIT_PORTFOLIO)
            applied.append("max_open_loss_cap_fraction")

    # The global envelope is the four-mode book's cap. Legacy strategies without
    # a mode_id stay on account-level limits and are not charged against it.
    if (
        intent.mode_id is not None
        and mode_book is not None
        and policy.max_global_open_risk is not None
    ):
        global_cap = policy.max_global_open_risk.to_money()
        if mode_book.total_open_risk() + recalculated_max_loss > global_cap:
            reasons.append(ReasonCode.RISK_LIMIT_PORTFOLIO)
            applied.append("max_global_open_risk")

    if reasons:
        return LimitEvaluation(
            passed=False,
            reason_codes=tuple(reasons),
            applied_limits=tuple(applied),
        )
    return LimitEvaluation(
        passed=True,
        reason_codes=(ReasonCode.OK,),
        applied_limits=(),
    )


def project_post_trade_exposure(
    portfolio: PortfolioSnapshot,
    *,
    margin_required: Money,
    premium_paid: Money,
    net_delta_delta: int,
) -> ExposureSnapshot:
    """Project portfolio exposure after a hypothetical fill."""
    exposure = portfolio.exposure
    return ExposureSnapshot(
        as_of=exposure.as_of,
        equity=exposure.equity,
        margin_used=exposure.margin_used + margin_required,
        margin_available=exposure.margin_available - margin_required,
        open_trade_count=exposure.open_trade_count + 1,
        net_delta=Decimal(exposure.net_delta) + Decimal(net_delta_delta),
        gross_notional=exposure.gross_notional + premium_paid,
        realized_pnl_today=exposure.realized_pnl_today,
        unrealized_pnl=exposure.unrealized_pnl,
    )


def evaluate_exposure_limits(
    report: ExposureReport,
    policy: RiskPolicyConfig,
) -> LimitEvaluation:
    """Hard ADESK-A4 / PART 6 portfolio caps. Pure arithmetic; no LLM."""
    reasons: list[ReasonCode] = []
    applied: list[str] = []
    equity = report.equity

    if abs(report.net_delta) > Decimal(policy.net_delta_limit):
        reasons.append(ReasonCode.RISK_LIMIT_PORTFOLIO)
        applied.append("net_delta_limit")

    if abs(report.net_vega) > policy.net_vega_limit:
        reasons.append(ReasonCode.RISK_LIMIT_PORTFOLIO)
        applied.append("net_vega_limit")

    if equity.amount > 0:
        expiry_cap = (equity * policy.expiry_day_notional_fraction).quantized(
            Rounding.FLOOR
        )
        for bucket in report.notional_by_expiry:
            if bucket.key == "NONE":
                continue
            if bucket.notional.amount > expiry_cap.amount:
                reasons.append(ReasonCode.CONCENTRATION_LIMIT)
                applied.append("expiry_day_notional_fraction")
                break

        event_cap = (equity * policy.single_event_exposure_fraction).quantized(
            Rounding.FLOOR
        )
        for overlap in report.event_overlaps:
            if overlap.notional.amount > event_cap.amount:
                reasons.append(ReasonCode.CONCENTRATION_LIMIT)
                applied.append("single_event_exposure_fraction")
                break

    if report.directional_agreement_ratio > policy.directional_agreement_max:
        reasons.append(ReasonCode.RISK_LIMIT_PORTFOLIO)
        applied.append("directional_agreement_max")

    if reasons:
        # Deduplicate reason codes while preserving order.
        seen: set[ReasonCode] = set()
        unique_reasons: list[ReasonCode] = []
        for code in reasons:
            if code not in seen:
                seen.add(code)
                unique_reasons.append(code)
        return LimitEvaluation(
            passed=False,
            reason_codes=tuple(unique_reasons),
            applied_limits=tuple(dict.fromkeys(applied)),
        )
    return LimitEvaluation(
        passed=True,
        reason_codes=(ReasonCode.OK,),
        applied_limits=(),
    )
