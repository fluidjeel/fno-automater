"""Deterministic cohort scorecard. No AI. No live mutation."""

from __future__ import annotations

import hashlib
from collections import Counter
from datetime import datetime
from decimal import Decimal

from trading.analytics.fills import simulate_fill
from trading.config.evaluation import FillModelConfig
from trading.domain.contracts.evaluation import (
    CohortPackage,
    CohortScorecard,
    CohortSignal,
    FillSimulation,
    ReasonCount,
)
from trading.domain.contracts.identification import ConfidenceKind
from trading.domain.enums import FillOutcome, ReasonCode, RiskAction, Severity, Side
from trading.domain.primitives import Currency, Money

_MIN_CONFIDENCE_SAMPLES = 2

__all__ = ["EvaluationError", "build_scorecard"]

_INR = Currency.INR


class EvaluationError(ValueError):
    """Raised when a cohort cannot be scored."""


def build_scorecard(
    package: CohortPackage,
    fill_policy: FillModelConfig,
    *,
    as_of: datetime,
) -> CohortScorecard:
    """Score one frozen experiment. Refuses mixed versions."""
    if not package.experiment.parameters_frozen:
        raise EvaluationError("unfrozen experiment cannot be scored")
    if fill_policy.version != package.experiment.fill_model_version:
        raise EvaluationError(
            f"fill-model {fill_policy.version} does not match experiment "
            f"{package.experiment.fill_model_version}; do not pool versions"
        )

    closed_pnls: list[Money] = []
    entry_slippages: list[Money] = []
    maes: list[Money] = []
    mfes: list[Money] = []
    reasons: Counter[ReasonCode] = Counter()
    strict_verdicts: Counter[ReasonCode] = Counter()
    regimes: set[str] = set()
    filled = partial = unfilled = rejects = declined = 0
    costs_confirmed = True
    total_charges = Money.zero(_INR)
    incident_p0_p1 = 0
    unreconciled = 0
    attempted = 0
    setup_feature_count = 0
    raw_score_count = 0
    calibrated: list[tuple[Decimal, Decimal]] = []

    for signal in package.signals:
        if signal.setup_features is not None:
            setup_feature_count += 1
            if (
                signal.setup_features.confidence_kind
                is ConfidenceKind.CALIBRATED_PROBABILITY
            ):
                if signal.judgment_label is not None:
                    calibrated.append(
                        (
                            signal.setup_features.raw_setup_score,
                            Decimal(1) if signal.judgment_label else Decimal(0),
                        )
                    )
            else:
                raw_score_count += 1
        if signal.incident_severity in {Severity.WARNING, Severity.CRITICAL}:
            incident_p0_p1 += 1
        if not signal.reconciled:
            unreconciled += 1
        if signal.regime is not None:
            regimes.add(signal.regime)
        if signal.declined:
            declined += 1
            if signal.rejection_reason is not None:
                reasons[signal.rejection_reason] += 1
            continue
        for code in signal.risk_reasons:
            reasons[code] += 1
        if (
            signal.rejection_reason is not None
            and signal.rejection_reason not in signal.risk_reasons
        ):
            reasons[signal.rejection_reason] += 1
        if signal.risk_action is RiskAction.REJECT:
            continue
        if signal.entry_command is None or signal.entry_quote is None:
            continue
        if signal.strict_fill_verdict is not None:
            strict_verdicts[signal.strict_fill_verdict] += 1
        attempted += 1
        entry = simulate_fill(
            signal.entry_command,
            signal.entry_quote,
            policy=fill_policy,
            traded_through=signal.traded_through_entry,
            charges_lots=signal.lots,
        )
        filled, partial, unfilled, rejects = _tally(
            entry, filled, partial, unfilled, rejects
        )
        costs_confirmed = costs_confirmed and _costs_ok(entry)
        if entry.outcome is not FillOutcome.FILLED or entry.fill_price is None:
            continue
        if entry.slippage is not None:
            entry_slippages.append(_scale(entry.slippage, entry.filled_quantity))
        if entry.charges is not None:
            total_charges = total_charges + entry.charges
        closed = _closed_pnl(signal, entry, fill_policy)
        if closed is None:
            continue
        pnl, exit_fill = closed
        costs_confirmed = costs_confirmed and _costs_ok(exit_fill)
        if exit_fill.charges is not None:
            total_charges = total_charges + exit_fill.charges
        closed_pnls.append(pnl)
        if signal.mae is not None:
            maes.append(signal.mae)
        if signal.mfe is not None:
            mfes.append(signal.mfe)

    gross = _sum(closed_pnls)
    net: Money | None = None
    expectancy: Money | None = None
    if costs_confirmed:
        net = gross - total_charges
        if closed_pnls:
            expectancy = Money.of(str(net.amount / Decimal(len(closed_pnls))), _INR)
    wins = [pnl for pnl in closed_pnls if pnl.amount > 0]
    losses = [pnl for pnl in closed_pnls if pnl.amount < 0]
    profit_factor = _profit_factor(wins, losses)
    share = _concentration(closed_pnls)
    window = package.observation_end - package.observation_start
    denom = Decimal(attempted) if attempted else Decimal(1)
    signal_denom = Decimal(len(package.signals)) if package.signals else Decimal(1)
    scorecard_id = hashlib.sha256(
        f"{package.experiment.experiment_id}|{fill_policy.version}|{as_of.isoformat()}".encode()
    ).hexdigest()[:16]
    histogram = tuple(
        ReasonCount(reason_code=code, count=count)
        for code, count in sorted(reasons.items(), key=lambda item: item[0].value)
    )
    strict_histogram = tuple(
        ReasonCount(reason_code=code, count=count)
        for code, count in sorted(
            strict_verdicts.items(), key=lambda item: item[0].value
        )
    )
    return CohortScorecard(
        scorecard_id=scorecard_id,
        experiment_id=package.experiment.experiment_id,
        fill_model_version=fill_policy.version,
        as_of=as_of,
        signal_count=len(package.signals),
        declined_count=declined,
        closed_trade_count=len(closed_pnls),
        filled_count=filled,
        partial_count=partial,
        unfilled_count=unfilled,
        reject_count=rejects,
        gross_pnl=gross,
        net_pnl=net,
        costs_confirmed=costs_confirmed,
        expectancy=expectancy,
        expectancy_lower_95=_expectancy_lower_95(closed_pnls, net),
        win_count=len(wins),
        loss_count=len(losses),
        average_win=_average(wins),
        average_loss=_average(losses),
        profit_factor=profit_factor,
        max_drawdown=_max_drawdown(closed_pnls),
        consecutive_losses=_max_consecutive_losses(closed_pnls),
        fill_rate=(Decimal(filled) / denom).quantize(Decimal("0.0001")),
        partial_fill_rate=(Decimal(partial) / denom).quantize(Decimal("0.0001")),
        reject_rate=(Decimal(rejects) / denom).quantize(Decimal("0.0001")),
        average_entry_slippage=_average(entry_slippages),
        average_mae=_average(maes),
        average_mfe=_average(mfes),
        reason_histogram=histogram,
        strict_fill_verdict_histogram=strict_histogram,
        regime_count=len(regimes),
        max_single_trade_pnl_share=share,
        incident_p0_p1_count=incident_p0_p1,
        unreconciled_count=unreconciled,
        observation_days=max(window.days, 0),
        setup_feature_count=setup_feature_count,
        raw_score_count=raw_score_count,
        calibrated_probability_count=len(calibrated),
        setup_coverage=(Decimal(setup_feature_count) / signal_denom).quantize(
            Decimal("0.0001")
        ),
        abstention_rate=(Decimal(declined) / signal_denom).quantize(Decimal("0.0001")),
        brier_score=_brier(calibrated),
    )


def _brier(values: list[tuple[Decimal, Decimal]]) -> Decimal | None:
    if not values:
        return None
    return (
        sum(
            ((probability - outcome) ** 2 for probability, outcome in values),
            Decimal(0),
        )
        / Decimal(len(values))
    ).quantize(Decimal("0.0001"))


def _tally(
    fill: FillSimulation,
    filled: int,
    partial: int,
    unfilled: int,
    rejects: int,
) -> tuple[int, int, int, int]:
    if fill.outcome is FillOutcome.FILLED:
        return filled + 1, partial, unfilled, rejects
    if fill.outcome is FillOutcome.PARTIAL:
        return filled, partial + 1, unfilled, rejects
    if fill.outcome is FillOutcome.UNFILLED:
        return filled, partial, unfilled + 1, rejects
    return filled, partial, unfilled, rejects + 1


def _costs_ok(fill: FillSimulation) -> bool:
    return fill.charges_confirmed or fill.outcome is not FillOutcome.FILLED


def _closed_pnl(
    signal: CohortSignal,
    entry: FillSimulation,
    fill_policy: FillModelConfig,
) -> tuple[Money, FillSimulation] | None:
    if (
        signal.exit_command is None
        or signal.exit_quote is None
        or entry.fill_price is None
    ):
        return None
    exit_fill = simulate_fill(
        signal.exit_command,
        signal.exit_quote,
        policy=fill_policy,
        traded_through=signal.traded_through_exit,
        charges_lots=signal.lots,
    )
    if exit_fill.outcome is not FillOutcome.FILLED or exit_fill.fill_price is None:
        return None
    qty = Decimal(min(entry.filled_quantity, exit_fill.filled_quantity))
    if signal.entry_command is None:
        return None
    if signal.entry_command.side is Side.BUY:
        raw = (exit_fill.fill_price.value - entry.fill_price.value) * qty
    else:
        raw = (entry.fill_price.value - exit_fill.fill_price.value) * qty
    return Money.of(str(raw), _INR), exit_fill


def _sum(values: list[Money]) -> Money:
    total = Money.zero(_INR)
    for value in values:
        total = total + value
    return total


def _average(values: list[Money]) -> Money | None:
    if not values:
        return None
    return Money.of(str(_sum(values).amount / Decimal(len(values))), _INR)


def _profit_factor(wins: list[Money], losses: list[Money]) -> Decimal | None:
    if not losses:
        return None
    loss_abs = abs(_sum(losses).amount)
    if loss_abs == 0:
        return None
    return (_sum(wins).amount / loss_abs).quantize(Decimal("0.0001"))


def _expectancy_lower_95(values: list[Money], net: Money | None) -> Money | None:
    if net is None or len(values) < _MIN_CONFIDENCE_SAMPLES:
        return None
    amounts = [value.amount for value in values]
    gross_mean = sum(amounts, Decimal(0)) / Decimal(len(amounts))
    variance = sum(
        ((amount - gross_mean) ** 2 for amount in amounts), Decimal(0)
    ) / Decimal(len(amounts) - 1)
    standard_error = variance.sqrt() / Decimal(len(amounts)).sqrt()
    net_mean = net.amount / Decimal(len(amounts))
    lower = net_mean - Decimal("1.96") * standard_error
    return Money.of(str(lower), _INR)


def _concentration(pnls: list[Money]) -> Decimal | None:
    if not pnls:
        return None
    abs_total = sum((abs(pnl.amount) for pnl in pnls), Decimal(0))
    if abs_total == 0:
        return Decimal(0)
    peak = max(abs(pnl.amount) for pnl in pnls)
    return (peak / abs_total).quantize(Decimal("0.0001"))


def _max_drawdown(pnls: list[Money]) -> Money:
    peak = Decimal(0)
    equity = Decimal(0)
    max_dd = Decimal(0)
    for pnl in pnls:
        equity += pnl.amount
        peak = max(peak, equity)
        drawdown = peak - equity
        max_dd = max(max_dd, drawdown)
    return Money.of(str(max_dd), _INR)


def _max_consecutive_losses(pnls: list[Money]) -> int:
    streak = best = 0
    for pnl in pnls:
        if pnl.amount < 0:
            streak += 1
            best = max(best, streak)
        else:
            streak = 0
    return best


def _scale(slippage: Money, quantity: int) -> Money:
    return Money.of(str(slippage.amount * Decimal(quantity)), slippage.currency)
