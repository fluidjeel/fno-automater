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
    "DiscoverySizingResult",
    "apply_discovery_lots",
    "collect_strict_would_block",
    "discovery_bug_guard_cap",
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


def apply_discovery_lots(
    *,
    cost_per_lot: Money,
    guide: Money,
) -> DiscoverySizingResult:
    """Strike first, size last: max(1, floor(guide / one_lot_max_loss))."""
    if cost_per_lot.is_zero or cost_per_lot.is_negative:
        return DiscoverySizingResult(
            approved_lots=0,
            recalculated_max_loss=Money.zero(cost_per_lot.currency),
            one_lot_over_guide=False,
        )
    guide_lots = floor_divide_money(guide, cost_per_lot)
    approved_lots = max(1, guide_lots)
    one_lot_over = cost_per_lot > guide
    recalculated = (cost_per_lot * approved_lots).quantized(Rounding.CEILING)
    return DiscoverySizingResult(
        approved_lots=approved_lots,
        recalculated_max_loss=recalculated,
        one_lot_over_guide=one_lot_over,
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
    """Hard DISCOVERY gates: bug guard and per-mode open-risk cap."""
    if approved_lots <= 0:
        return LimitEvaluation(
            passed=False,
            reason_codes=(ReasonCode.SIZE_BELOW_MINIMUM,),
            applied_limits=("minimum_lot",),
        )
    reasons: list[ReasonCode] = []
    applied: list[str] = []
    bug_cap = discovery_bug_guard_cap(mode_ledger, discovery)
    if cost_per_lot > bug_cap:
        reasons.append(ReasonCode.RISK_LIMIT_TRADE)
        applied.append("bug_guard_trade_risk_fraction")
    open_cap = discovery_open_risk_cap(mode_ledger, discovery, mode_id)
    projected_open = mode_ledger.open_risk + recalculated_max_loss
    if projected_open > open_cap:
        reasons.append(ReasonCode.RISK_LIMIT_PORTFOLIO)
        applied.append("open_risk_cap")
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
    return tuple(
        dict.fromkeys(code for code in codes if code is not ReasonCode.OK)
    )


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
