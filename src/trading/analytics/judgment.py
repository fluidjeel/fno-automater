"""Offline judgment harness: label should_enter/should_pass from MAE/MFE - charges."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal

from trading.config.evaluation import FillModelConfig, JudgmentThresholds
from trading.domain.contracts.evaluation import (
    CohortPackage,
    CohortSignal,
    JudgmentReport,
    JudgmentSignalResult,
)
from trading.domain.contracts.identification import ConfidenceKind
from trading.domain.primitives import Money

__all__ = ["JudgmentError", "evaluate_judgment", "label_signal"]


class JudgmentError(ValueError):
    """Raised when a cohort cannot be judged."""


def label_signal(
    signal: CohortSignal,
    *,
    charges_per_lot: Decimal,
    thresholds: JudgmentThresholds,
) -> bool | None:
    """Return True=should_enter, False=should_pass, None=unlabeled."""
    if signal.declined or signal.mae is None or signal.mfe is None:
        return None
    charges = charges_per_lot * Decimal(signal.lots)
    net_mfe = signal.mfe.amount - charges
    if net_mfe < thresholds.min_net_mfe_amount:
        return False
    return not (thresholds.require_mfe_beats_mae and net_mfe <= signal.mae.amount)


def evaluate_judgment(
    package: CohortPackage,
    fill_model: FillModelConfig,
    thresholds: JudgmentThresholds,
    *,
    as_of: datetime,
) -> JudgmentReport:
    """Score offline enter/pass labels and desk precision/capture/Brier."""
    if not package.experiment.parameters_frozen:
        raise JudgmentError("unfrozen experiment cannot be judged")
    charges_per_lot = fill_model.charges_per_lot.require("fill_model.charges_per_lot")
    currency = thresholds.currency

    rows: list[JudgmentSignalResult] = []
    for signal in package.signals:
        label = label_signal(
            signal,
            charges_per_lot=charges_per_lot,
            thresholds=thresholds,
        )
        did_enter = bool(
            not signal.declined
            and signal.executed
            and signal.mae is not None
            and signal.mfe is not None
        )
        setup_confidence: Decimal | None = None
        if (
            signal.setup_features is not None
            and signal.setup_features.confidence_kind
            is ConfidenceKind.CALIBRATED_PROBABILITY
        ):
            setup_confidence = Decimal(signal.setup_features.raw_setup_score)
        agent_confidence = (
            Decimal(signal.agent_confidence)
            if signal.agent_confidence is not None
            else None
        )
        charges = (
            None
            if label is None
            else Money.of(charges_per_lot * Decimal(signal.lots), currency)
        )
        net_mfe = None
        if signal.mfe is not None and label is not None:
            net_mfe = Money.of(
                signal.mfe.amount - charges_per_lot * Decimal(signal.lots),
                currency,
            )
        rows.append(
            JudgmentSignalResult(
                signal_id=signal.signal_id,
                should_enter=label,
                entered=did_enter,
                confidence=setup_confidence,
                agent_confidence=agent_confidence,
                mae=signal.mae,
                mfe=signal.mfe,
                charges=charges,
                net_mfe=net_mfe,
            )
        )

    labeled = [row for row in rows if row.should_enter is not None]
    should_enter_rows = [row for row in labeled if row.should_enter]
    should_pass_rows = [row for row in labeled if not row.should_enter]
    entered_rows = [row for row in labeled if row.entered]
    true_positive = [row for row in entered_rows if row.should_enter]
    false_positive = [row for row in entered_rows if not row.should_enter]
    false_negative = [row for row in should_enter_rows if not row.entered]

    precision = _ratio(len(true_positive), len(entered_rows))
    capture = _ratio(len(true_positive), len(should_enter_rows))
    setup_brier = _brier(labeled, lambda row: row.confidence)
    setup_reliability = _brier_reliability(labeled, lambda row: row.confidence)
    agent_brier = _brier(labeled, lambda row: row.agent_confidence)
    agent_reliability = _brier_reliability(labeled, lambda row: row.agent_confidence)

    gates: list[str] = []
    if precision is None or precision < thresholds.min_precision:
        gates.append("min_precision")
    if capture is None or capture < thresholds.min_capture:
        gates.append("min_capture")
    if setup_brier is None:
        gates.append("brier_unavailable")
    elif setup_brier > thresholds.max_brier:
        gates.append("max_brier")
    if any(row.agent_confidence is not None for row in labeled) and agent_brier is None:
        gates.append("agent_brier_unavailable")

    return JudgmentReport(
        experiment_id=package.experiment.experiment_id,
        as_of=as_of,
        fill_model_version=fill_model.version,
        labeled_count=len(labeled),
        should_enter_count=len(should_enter_rows),
        should_pass_count=len(should_pass_rows),
        entered_count=len(entered_rows),
        true_positive_count=len(true_positive),
        false_positive_count=len(false_positive),
        false_negative_count=len(false_negative),
        precision=precision,
        capture=capture,
        brier_score=setup_brier,
        setup_brier_reliability=setup_reliability,
        agent_brier_score=agent_brier,
        agent_brier_reliability=agent_reliability,
        failed_gate_ids=tuple(gates),
        signals=tuple(rows),
    )


def _ratio(numerator: int, denominator: int) -> Decimal | None:
    if denominator <= 0:
        return None
    return (Decimal(numerator) / Decimal(denominator)).quantize(Decimal("0.0001"))


def _brier(
    rows: list[JudgmentSignalResult],
    confidence_of: Callable[[JudgmentSignalResult], Decimal | None],
) -> Decimal | None:
    squares: list[Decimal] = []
    for row in rows:
        confidence = confidence_of(row)
        if row.should_enter is None or confidence is None:
            continue
        outcome = Decimal(1) if row.should_enter else Decimal(0)
        squares.append((confidence - outcome) ** 2)
    if not squares:
        return None
    total = sum(squares, start=Decimal(0))
    return (total / Decimal(len(squares))).quantize(Decimal("0.0001"))


def _brier_reliability(
    rows: list[JudgmentSignalResult],
    confidence_of: Callable[[JudgmentSignalResult], Decimal | None],
) -> Decimal | None:
    """Murphy reliability component of the Brier score."""
    buckets: dict[int, list[tuple[Decimal, Decimal]]] = defaultdict(list)
    for row in rows:
        confidence = confidence_of(row)
        if row.should_enter is None or confidence is None:
            continue
        outcome = Decimal(1) if row.should_enter else Decimal(0)
        bucket = min(4, int(confidence * Decimal(5)))
        buckets[bucket].append((confidence, outcome))
    if not buckets:
        return None
    total = 0
    reliability = Decimal(0)
    for members in buckets.values():
        if not members:
            continue
        forecasts = [item[0] for item in members]
        outcomes = [item[1] for item in members]
        mean_forecast = sum(forecasts, start=Decimal(0)) / Decimal(len(forecasts))
        mean_outcome = sum(outcomes, start=Decimal(0)) / Decimal(len(outcomes))
        reliability += Decimal(len(members)) * (mean_forecast - mean_outcome) ** 2
        total += len(members)
    if total <= 0:
        return None
    return (reliability / Decimal(total)).quantize(Decimal("0.0001"))
