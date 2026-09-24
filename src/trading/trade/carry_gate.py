"""Mode 2 overnight carry gate (Phase P9 / spec §4.2, §2.15).

Replaces the legacy profit-and-conviction ``evaluate_carry_forward`` path for
Mode 2. A positive mark-to-market or trailing stop alone is insufficient.
A losing trade does not receive a looser standard.
"""

from __future__ import annotations

import importlib
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from trading.domain.contracts.carry import (
    CarryGateDecision,
    M2CarryGateConfig,
    M2CarryGateInput,
)
from trading.domain.contracts.identification import MarketState, TrendState
from trading.domain.contracts.intent import TradeIntent
from trading.domain.contracts.position import PositionState
from trading.domain.enums import CarryGateAction, FamilyId, ModeId, ReasonCode
from trading.domain.primitives import Money

__all__ = [
    "assess_m2_thesis",
    "build_m2_carry_gate_input",
    "evaluate_m2_carry_gate",
    "load_carry_gate_config",
    "overnight_loss_budget",
]

Path = Any


def load_carry_gate_config(path: Path | None = None) -> M2CarryGateConfig:
    """Load validated carry gate configuration from YAML."""
    path_cls = importlib.import_module("pathlib").Path
    yaml_mod = importlib.import_module("yaml")

    if path is None:
        repo_config = (
            path_cls(__file__).resolve().parents[3] / "config" / "carry_gate.yaml"
        )
        target_path = (
            repo_config if repo_config.is_file() else path_cls("config/carry_gate.yaml")
        )
    else:
        target_path = path_cls(path)

    raw: Any = yaml_mod.safe_load(target_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"expected mapping in {target_path}, got {type(raw).__name__}")
    return M2CarryGateConfig.model_validate(raw)


def overnight_loss_budget(
    mode_reference_capital: Money, config: M2CarryGateConfig
) -> Money:
    """Compute the overnight structure-loss budget for one Mode 2 position."""
    amount = (
        mode_reference_capital.amount * config.overnight_loss_budget_fraction
    ).quantize(Decimal("0.01"))
    return Money(amount, mode_reference_capital.currency)


def assess_m2_thesis(
    intent: TradeIntent,
    *,
    market: MarketState | None,
    session_date: date,
) -> tuple[bool, bool]:
    """Return ``(thesis_evaluated_today, thesis_still_valid)`` for carry evidence."""
    features = intent.setup_features
    if features is None or market is None:
        return False, False
    evaluated_today = (
        market.calculated_at.astimezone(market.calculated_at.tzinfo).date()
        == session_date
    )
    family = intent.family_id
    trend = market.trend
    if family == FamilyId.long_call.value:
        still_valid = trend in {TrendState.UP, TrendState.RANGE}
    elif family == FamilyId.long_put.value:
        still_valid = trend in {TrendState.DOWN, TrendState.RANGE}
    else:
        still_valid = False
    return evaluated_today, still_valid


def build_m2_carry_gate_input(
    *,
    position: PositionState,
    intent: TradeIntent,
    market: MarketState | None,
    session_date: date,
    mode_reference_capital: Money,
    config: M2CarryGateConfig,
    event_blackout: bool,
    portfolio_entries_blocked: bool,
    mode_daily_loss_breached: bool,
    recovery_healthy: bool,
    current_pnl: Money | None = None,
    remaining_dte: int,
) -> M2CarryGateInput:
    """Assemble carry evidence from runtime state at entry cutoff."""
    mode_id = position.mode_id or intent.mode_id or ModeId.M2_DIRECTIONAL
    thesis_today, thesis_valid = assess_m2_thesis(
        intent, market=market, session_date=session_date
    )
    exit_persisted = position.exit_policy.initial_stop_distance_ticks > 0 and (
        position.exit_policy.stop_price is not None
        or position.exit_policy.pnl_stop is not None
    )
    return M2CarryGateInput(
        mode_id=mode_id,
        thesis_evaluated_today=thesis_today,
        thesis_still_valid=thesis_valid,
        remaining_dte=remaining_dte,
        overnight_max_loss=intent.estimated_max_loss,
        overnight_loss_budget=overnight_loss_budget(mode_reference_capital, config),
        event_blackout=event_blackout,
        portfolio_entries_blocked=portfolio_entries_blocked,
        mode_daily_loss_breached=mode_daily_loss_breached,
        exit_policy_persisted=exit_persisted,
        recovery_healthy=recovery_healthy,
        current_pnl=current_pnl,
    )


def evaluate_m2_carry_gate(
    *,
    trade_id: str,
    session_date: date,
    inputs: M2CarryGateInput,
    config: M2CarryGateConfig,
    as_of: datetime,
    decision_id: str,
) -> CarryGateDecision:
    """Return ``CARRY_APPROVED`` only when every required evidence check passes."""
    if inputs.mode_id is not ModeId.M2_DIRECTIONAL:
        return _decision(
            trade_id=trade_id,
            session_date=session_date,
            action=CarryGateAction.CARRY_REJECTED,
            mode_id=inputs.mode_id,
            reason_code=ReasonCode.CARRY_MODE_MISMATCH,
            detail="carry gate applies to Mode 2 positions only",
            exit_initiated=False,
            as_of=as_of,
            decision_id=decision_id,
        )

    checks: tuple[tuple[bool, ReasonCode, str], ...] = (
        (
            inputs.thesis_evaluated_today and inputs.thesis_still_valid,
            ReasonCode.CARRY_THESIS_STALE
            if not inputs.thesis_evaluated_today
            else ReasonCode.CARRY_THESIS_INVALID,
            "thesis not freshly evaluated today"
            if not inputs.thesis_evaluated_today
            else "thesis driver no longer valid for the open family",
        ),
        (
            inputs.remaining_dte >= config.min_remaining_dte,
            ReasonCode.CARRY_INSUFFICIENT_DTE,
            f"remaining DTE {inputs.remaining_dte} below minimum "
            f"{config.min_remaining_dte}",
        ),
        (
            inputs.overnight_max_loss <= inputs.overnight_loss_budget,
            ReasonCode.CARRY_OVERNIGHT_BUDGET,
            "overnight structure-loss exceeds the mode budget",
        ),
        (
            not inputs.event_blackout,
            ReasonCode.CARRY_EVENT_BLACKOUT,
            "event blackout active",
        ),
        (
            not inputs.portfolio_entries_blocked,
            ReasonCode.CARRY_PORTFOLIO_BLOCKED,
            "portfolio entries blocked",
        ),
        (
            not inputs.mode_daily_loss_breached,
            ReasonCode.CARRY_PORTFOLIO_BLOCKED,
            "mode daily loss budget breached",
        ),
        (
            inputs.exit_policy_persisted,
            ReasonCode.CARRY_EXIT_STATE_MISSING,
            "persisted exit policy missing or incomplete",
        ),
        (
            inputs.recovery_healthy,
            ReasonCode.CARRY_RECOVERY_UNHEALTHY,
            "next-session recovery health check failed",
        ),
    )
    for ok, reason_code, detail in checks:
        if ok:
            continue
        return _decision(
            trade_id=trade_id,
            session_date=session_date,
            action=CarryGateAction.CARRY_REJECTED,
            mode_id=inputs.mode_id,
            reason_code=reason_code,
            detail=detail,
            exit_initiated=True,
            as_of=as_of,
            decision_id=decision_id,
        )

    return _decision(
        trade_id=trade_id,
        session_date=session_date,
        action=CarryGateAction.CARRY_APPROVED,
        mode_id=inputs.mode_id,
        reason_code=ReasonCode.OK,
        detail="all required carry evidence passed",
        exit_initiated=False,
        as_of=as_of,
        decision_id=decision_id,
    )


def _decision(
    *,
    trade_id: str,
    session_date: date,
    action: CarryGateAction,
    mode_id: ModeId,
    reason_code: ReasonCode,
    detail: str,
    exit_initiated: bool,
    as_of: datetime,
    decision_id: str,
) -> CarryGateDecision:
    return CarryGateDecision(
        decision_id=decision_id,
        trade_id=trade_id,
        session_date=session_date,
        action=action,
        mode_id=mode_id,
        reason_code=reason_code,
        detail=detail,
        exit_initiated=exit_initiated,
        as_of=as_of,
    )
