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

__all__ = [
    "JudgmentError",
    "evaluate_judgment",
    "label_excursion",
    "label_signal",
]


class JudgmentError(ValueError):
    """Raised when a cohort cannot be judged."""


def label_excursion(
    *,
    mae: Decimal,
    mfe: Decimal,
    lots: int,
    charges_per_lot: Decimal,
    thresholds: JudgmentThresholds,
) -> bool:
    """True=should_enter when net MFE clears charges and beats MAE."""
    charges = charges_per_lot * Decimal(lots)
    net_mfe = mfe - charges
    if net_mfe < thresholds.min_net_mfe_amount:
        return False
    return not (thresholds.require_mfe_beats_mae and net_mfe <= mae)


def label_signal(
    signal: CohortSignal,
    *,
    charges_per_lot: Decimal,
    thresholds: JudgmentThresholds,
) -> bool | None:
    """Return True=should_enter, False=should_pass, None=unlabeled."""
    if signal.declined or signal.mae is None or signal.mfe is None:
        return None
    return label_excursion(
        mae=signal.mae.amount,
        mfe=signal.mfe.amount,
        lots=signal.lots,
        charges_per_lot=charges_per_lot,
        thresholds=thresholds,
    )


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
    enter_rows = [row for row in labeled if row.should_enter]
    pass_rows = [row for row in labeled if not row.should_enter]
    taken_rows = [row for row in labeled if row.entered]
    true_positive = [row for row in taken_rows if row.should_enter]
    false_positive = [row for row in taken_rows if not row.should_enter]
    false_negative = [row for row in enter_rows if not row.entered]

    precision = _ratio(len(true_positive), len(taken_rows))
    capture = _ratio(len(true_positive), len(enter_rows))
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
        should_enter_count=len(enter_rows),
        should_pass_count=len(pass_rows),
        entered_count=len(taken_rows),
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
    total = Decimal(0)
    for row in scored:
        confidence = row.confidence
        if confidence is None:
            continue
        outcome = Decimal(1) if row.should_enter else Decimal(0)
        total += (confidence - outcome) ** 2
    return (total / Decimal(len(scored))).quantize(Decimal("0.0001"))
