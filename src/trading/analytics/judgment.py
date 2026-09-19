"""Offline judgment harness: label should_enter/should_pass from MAE/MFE - charges."""

from __future__ import annotations

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
        entered = bool(
            not signal.declined
            and signal.executed
            and signal.mae is not None
            and signal.mfe is not None
        )
        confidence: Decimal | None = None
        if (
            signal.setup_features is not None
            and signal.setup_features.confidence_kind
            is ConfidenceKind.CALIBRATED_PROBABILITY
        ):
            confidence = Decimal(signal.setup_features.raw_setup_score)
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
                entered=entered,
                confidence=confidence,
                mae=signal.mae,
                mfe=signal.mfe,
                charges=charges,
                net_mfe=net_mfe,
            )
        )

    labeled = [row for row in rows if row.should_enter is not None]
    should_enter = [row for row in labeled if row.should_enter]
    should_pass = [row for row in labeled if not row.should_enter]
    entered = [row for row in labeled if row.entered]
    true_positive = [row for row in entered if row.should_enter]
    false_positive = [row for row in entered if not row.should_enter]
    false_negative = [row for row in should_enter if not row.entered]

    precision = _ratio(len(true_positive), len(entered))
    capture = _ratio(len(true_positive), len(should_enter))
    brier = _brier(labeled)

    gates: list[str] = []
    if precision is None or precision < thresholds.min_precision:
        gates.append("min_precision")
    if capture is None or capture < thresholds.min_capture:
        gates.append("min_capture")
    if brier is None:
        gates.append("brier_unavailable")
    elif brier > thresholds.max_brier:
        gates.append("max_brier")

    return JudgmentReport(
        experiment_id=package.experiment.experiment_id,
        as_of=as_of,
        fill_model_version=fill_model.version,
        labeled_count=len(labeled),
        should_enter_count=len(should_enter),
        should_pass_count=len(should_pass),
        entered_count=len(entered),
        true_positive_count=len(true_positive),
        false_positive_count=len(false_positive),
        false_negative_count=len(false_negative),
        precision=precision,
        capture=capture,
        brier_score=brier,
        failed_gate_ids=tuple(gates),
        signals=tuple(rows),
    )


def _ratio(numerator: int, denominator: int) -> Decimal | None:
    if denominator <= 0:
        return None
    return (Decimal(numerator) / Decimal(denominator)).quantize(Decimal("0.0001"))


def _brier(rows: list[JudgmentSignalResult]) -> Decimal | None:
    scored = [
        row
        for row in rows
        if row.should_enter is not None and row.confidence is not None
    ]
    if not scored:
        return None
    total = sum(
        (row.confidence - (Decimal(1) if row.should_enter else Decimal(0))) ** 2
        for row in scored
    )
    return (total / Decimal(len(scored))).quantize(Decimal("0.0001"))
