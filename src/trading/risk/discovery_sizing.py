"""DISCOVERY profile sizing and limit evaluation (DISC-A2)."""

from __future__ import annotations

from dataclasses import dataclass

from trading.config.discovery import DiscoveryConfig
from trading.config.risk_policy import RiskPolicyConfig
from trading.config.schema import RiskLimits
from trading.domain.contracts.exposure import ExposureReport
from trading.domain.contracts.intent import TradeIntent
from trading.domain.contracts.mode_policy import ModesConfig
from trading.domain.contracts.portfolio import PortfolioSnapshot
from trading.domain.contracts.risk import ApprovedLeg
from trading.domain.contracts.sizing import SizingLimits
from trading.domain.enums import ModeId, ReasonCode
from trading.domain.primitives import Lots, Money, Rounding
from trading.portfolio.campaign_drawdown import CampaignLedger
from trading.risk.limits import (
    LimitEvaluation,
    evaluate_campaign_limit,
    evaluate_exposure_limits,
    evaluate_pre_trade_limits,
    floor_divide_money,
)
from trading.risk.mode_ledger import FourModeBook, ModeLedger

__all__ = [
    "DiscoveryCapSizingResult",
    "DiscoveryMarginSizingResult",
    "DiscoverySizingResult",
    "DiscoveryTradeLotCapResult",
    "apply_discovery_cap_sizing",
    "apply_discovery_lots",
    "apply_discovery_margin_sizing",
    "apply_discovery_trade_lot_cap",
    "collect_strict_would_block",
    "discovery_bug_guard_cap",
    "discovery_effective_cost_per_lot",
    "discovery_guide_budget",
    "discovery_open_risk_cap",
    "evaluate_discovery_hard_limits",
    "rescale_approved_legs",
]


@dataclass(frozen=True, slots=True)
class DiscoverySizingResult:
    """Lots and loss after the discovery guide formula."""

    approved_lots: int
    recalculated_max_loss: Money
    one_lot_over_guide: bool


@dataclass(frozen=True, slots=True)
class DiscoveryCapSizingResult:
    """Lots after soft cap downsizing plus strict_would_block shadows."""

    approved_lots: int
    recalculated_max_loss: Money
    strict_would_block: tuple[ReasonCode, ...]
    applied_limits: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DiscoveryTradeLotCapResult:
    """Lots after the per-trade soft cap."""

    approved_lots: int
    recalculated_max_loss: Money
    applied_limits: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DiscoveryMarginSizingResult:
    """Lots and margin after soft broker-margin downsizing."""

    approved_lots: int
    recalculated_max_loss: Money
    estimated_margin: Money
    strict_would_block: tuple[ReasonCode, ...]
    applied_limits: tuple[str, ...]
    margin_oversubscribed: bool


def discovery_guide_budget(
    mode_ledger: ModeLedger,
    discovery: DiscoveryConfig,
    mode_id: ModeId,
) -> Money:
    """Per-trade guide budget for one mode."""
    mode_cfg = discovery.modes[mode_id.value]
    return (mode_ledger.reference_capital * mode_cfg.per_trade_guide).quantized(
        Rounding.FLOOR
    )


def discovery_open_risk_cap(
    mode_ledger: ModeLedger,
    discovery: DiscoveryConfig,
    mode_id: ModeId,
) -> Money:
    """Per-mode open-risk ceiling from discovery config."""
    mode_cfg = discovery.modes[mode_id.value]
    return (mode_ledger.reference_capital * mode_cfg.open_risk_cap).quantized(
        Rounding.FLOOR
    )


def discovery_bug_guard_cap(
    mode_ledger: ModeLedger,
    discovery: DiscoveryConfig,
) -> Money:
    """Maximum one-lot loss before the bug guard hard-blocks."""
    return (
        mode_ledger.reference_capital * discovery.bug_guard_trade_risk_fraction
    ).quantized(Rounding.FLOOR)


def discovery_effective_cost_per_lot(
    cost_per_lot: Money,
    margin_per_lot: Money,
) -> Money:
    """Per-lot budget unit: max(estimated max loss, broker margin)."""
    if margin_per_lot.is_zero:
        return cost_per_lot
    if cost_per_lot > margin_per_lot:
        return cost_per_lot
    return margin_per_lot


def apply_discovery_lots(
    *,
    cost_per_lot: Money,
    guide: Money,
    margin_per_lot: Money | None = None,
) -> DiscoverySizingResult:
    """Strike first, size last: max(1, floor(guide / one_lot_max_loss))."""
    effective = discovery_effective_cost_per_lot(
        cost_per_lot,
        margin_per_lot or Money.zero(cost_per_lot.currency),
    )
    if effective.is_zero or effective.is_negative:
        return DiscoverySizingResult(
            approved_lots=0,
            recalculated_max_loss=Money.zero(cost_per_lot.currency),
            one_lot_over_guide=False,
        )
    guide_lots = floor_divide_money(guide, effective)
    approved_lots = max(1, guide_lots)
    one_lot_over = effective > guide
    recalculated = (cost_per_lot * approved_lots).quantized(Rounding.CEILING)
    return DiscoverySizingResult(
        approved_lots=approved_lots,
        recalculated_max_loss=recalculated,
        one_lot_over_guide=one_lot_over,
    )


def apply_discovery_trade_lot_cap(
    *,
    approved_lots: int,
    recalculated_max_loss: Money,
    cost_per_lot: Money,
    max_lots_per_trade: int,
) -> DiscoveryTradeLotCapResult:
    """Downsize to the per-trade soft cap; never reject when one lot fits."""
    zero = Money.zero(cost_per_lot.currency)
    if approved_lots <= 0:
        return DiscoveryTradeLotCapResult(
            approved_lots=0,
            recalculated_max_loss=zero,
            applied_limits=(),
        )
    lots = approved_lots
    applied: list[str] = []
    if lots > max_lots_per_trade:
        lots = max_lots_per_trade
        applied.append("max_lots_per_trade")
    recalculated = (cost_per_lot * lots).quantized(Rounding.CEILING)
    return DiscoveryTradeLotCapResult(
        approved_lots=lots,
        recalculated_max_loss=recalculated,
        applied_limits=tuple(applied),
    )


def apply_discovery_cap_sizing(
    *,
    cost_per_lot: Money,
    recalculated_max_loss: Money,
    approved_lots: int,
    mode_ledger: ModeLedger,
    discovery: DiscoveryConfig,
    mode_id: ModeId,
) -> DiscoveryCapSizingResult:
    """Downsize to soft caps; never reject when at least one lot is affordable."""
    zero = Money.zero(cost_per_lot.currency)
    if approved_lots <= 0:
        return DiscoveryCapSizingResult(
            approved_lots=0,
            recalculated_max_loss=zero,
            strict_would_block=(),
            applied_limits=(),
        )
    shadow: list[ReasonCode] = []
    applied: list[str] = []
    lots = approved_lots
    bug_cap = discovery_bug_guard_cap(mode_ledger, discovery)
    if cost_per_lot > bug_cap:
        lots = 1
        shadow.append(ReasonCode.RISK_LIMIT_TRADE)
        applied.append("bug_guard_trade_risk_fraction")
    open_cap = discovery_open_risk_cap(mode_ledger, discovery, mode_id)
    remaining = open_cap - mode_ledger.open_risk
    if remaining.is_zero or remaining.is_negative:
        lots = max(1, min(lots, 1))
        shadow.append(ReasonCode.RISK_LIMIT_PORTFOLIO)
        applied.append("open_risk_cap")
    else:
        cap_lots = max(1, floor_divide_money(remaining, cost_per_lot))
        if cap_lots < lots:
            lots = cap_lots
            applied.append("open_risk_cap")
            if (cost_per_lot * lots).quantized(Rounding.CEILING) > remaining:
                shadow.append(ReasonCode.RISK_LIMIT_PORTFOLIO)
    recalculated = (cost_per_lot * lots).quantized(Rounding.CEILING)
    projected = mode_ledger.open_risk + recalculated
    if projected > open_cap and ReasonCode.RISK_LIMIT_PORTFOLIO not in shadow:
        shadow.append(ReasonCode.RISK_LIMIT_PORTFOLIO)
        if "open_risk_cap" not in applied:
            applied.append("open_risk_cap")
    return DiscoveryCapSizingResult(
        approved_lots=lots,
        recalculated_max_loss=recalculated,
        strict_would_block=tuple(dict.fromkeys(shadow)),
        applied_limits=tuple(applied),
    )


def apply_discovery_margin_sizing(
    *,
    approved_lots: int,
    recalculated_max_loss: Money,
    estimated_margin: Money,
    cost_per_lot: Money,
    margin_per_lot: Money,
    margin_lots: int,
    broker_margin_available: Money,
) -> DiscoveryMarginSizingResult:
    """Downsize to min 1 lot when broker margin binds; never reject."""
    zero = Money.zero(cost_per_lot.currency)
    if approved_lots <= 0:
        return DiscoveryMarginSizingResult(
            approved_lots=0,
            recalculated_max_loss=zero,
            estimated_margin=zero,
            strict_would_block=(),
            applied_limits=(),
            margin_oversubscribed=False,
        )
    shadow: list[ReasonCode] = []
    applied: list[str] = []
    lots = approved_lots
    effective_margin_per_lot = margin_per_lot
    if effective_margin_per_lot.is_zero:
        effective_margin_per_lot = (
            estimated_margin / approved_lots
            if approved_lots > 0 and not estimated_margin.is_zero
            else zero
        )
    if not effective_margin_per_lot.is_zero:
        affordable = max(
            1, floor_divide_money(broker_margin_available, effective_margin_per_lot)
        )
        if affordable < lots:
            lots = affordable
            shadow.append(ReasonCode.MARGIN_INSUFFICIENT)
            applied.append("broker_margin_available")
    elif margin_lots <= 0:
        lots = max(1, min(lots, 1))
        shadow.append(ReasonCode.MARGIN_INSUFFICIENT)
        applied.append("broker_margin_available")
    recalculated = (cost_per_lot * lots).quantized(Rounding.CEILING)
    margin_required = (
        (effective_margin_per_lot * lots).quantized(Rounding.CEILING)
        if not effective_margin_per_lot.is_zero
        else estimated_margin
    )
    if margin_required.is_zero and not recalculated.is_zero:
        margin_required = recalculated
    oversubscribed = margin_required > broker_margin_available
    if oversubscribed and ReasonCode.MARGIN_OVERSUBSCRIBED not in shadow:
        shadow.append(ReasonCode.MARGIN_OVERSUBSCRIBED)
        if "broker_margin_available" not in applied:
            applied.append("broker_margin_available")
    return DiscoveryMarginSizingResult(
        approved_lots=lots,
        recalculated_max_loss=recalculated,
        estimated_margin=margin_required,
        strict_would_block=tuple(dict.fromkeys(shadow)),
        applied_limits=tuple(applied),
        margin_oversubscribed=oversubscribed,
    )


def evaluate_discovery_hard_limits(
    *,
    cost_per_lot: Money,
    recalculated_max_loss: Money,
    approved_lots: int,
    mode_ledger: ModeLedger,
    discovery: DiscoveryConfig,
    mode_id: ModeId,
) -> LimitEvaluation:
    """DISCOVERY cap guide: downsize and record strict_would_block; never reject."""
    if approved_lots <= 0:
        return LimitEvaluation(
            passed=False,
            reason_codes=(ReasonCode.SIZE_BELOW_MINIMUM,),
            applied_limits=("minimum_lot",),
        )
    capped = apply_discovery_cap_sizing(
        cost_per_lot=cost_per_lot,
        recalculated_max_loss=recalculated_max_loss,
        approved_lots=approved_lots,
        mode_ledger=mode_ledger,
        discovery=discovery,
        mode_id=mode_id,
    )
    if capped.approved_lots <= 0:
        return LimitEvaluation(
            passed=False,
            reason_codes=(ReasonCode.SIZE_BELOW_MINIMUM,),
            applied_limits=("minimum_lot",),
        )
    return LimitEvaluation(
        passed=True,
        reason_codes=(ReasonCode.OK,),
        applied_limits=capped.applied_limits,
    )


def collect_strict_would_block(
    intent: TradeIntent,
    portfolio: PortfolioSnapshot,
    account_risk: RiskLimits,
    policy: RiskPolicyConfig,
    limits: SizingLimits,
    *,
    recalculated_max_loss: Money,
    approved_lots: int,
    mode_ledger: ModeLedger | None,
    modes_config: ModesConfig | None,
    mode_book: FourModeBook | None,
    exposure_report: ExposureReport | None,
    campaign_id: str | None,
    campaign_ledger: CampaignLedger | None,
) -> tuple[ReasonCode, ...]:
    """Reason codes the STRICT profile would have rejected with."""
    codes: list[ReasonCode] = []
    strict_limits = evaluate_pre_trade_limits(
        intent,
        portfolio,
        account_risk,
        policy,
        limits,
        recalculated_max_loss=recalculated_max_loss,
        approved_lots=approved_lots,
        mode_ledger=mode_ledger,
        modes_config=modes_config,
        mode_book=mode_book,
    )
    if not strict_limits.passed:
        codes.extend(strict_limits.reason_codes)
    if exposure_report is not None:
        exposure_check = evaluate_exposure_limits(exposure_report, policy)
        if not exposure_check.passed:
            codes.extend(exposure_check.reason_codes)
    campaign_check = evaluate_campaign_limit(
        campaign_id,
        campaign_ledger,
        recalculated_max_loss=recalculated_max_loss,
    )
    if not campaign_check.passed:
        codes.extend(campaign_check.reason_codes)
    return tuple(dict.fromkeys(code for code in codes if code is not ReasonCode.OK))


def rescale_approved_legs(
    approved_legs: tuple[ApprovedLeg, ...],
    approved_lots: int,
) -> tuple[ApprovedLeg, ...]:
    """Rebuild approved legs after discovery lot rescaling."""
    if approved_lots <= 0:
        return ()
    return tuple(
        ApprovedLeg(
            leg_id=leg.leg_id,
            lots=Lots(approved_lots),
            lot_size=leg.lot_size,
        )
        for leg in approved_legs
    )
