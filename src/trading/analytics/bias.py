"""Deterministic bias battery (ADESK-A6 / AGENT_DESK_SPEC PART 9.3).

Pure functions over trade + decision records. No LLM. All 11 PART 9.3 metrics
are always present (value may be None when under-sampled).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from math import sqrt
from statistics import median

from trading.domain.contracts.bias import BiasMetricId, BiasMetricResult, BiasReport

__all__ = [
    "BiasBatteryBands",
    "DecisionBiasInput",
    "TradeOutcomeInput",
    "WarmColdPair",
    "build_bias_report",
]

_ALL_METRICS: tuple[BiasMetricId, ...] = tuple(BiasMetricId)


@dataclass(frozen=True, slots=True)
class TradeOutcomeInput:
    """Closed-trade facts the battery needs."""

    trade_id: str
    is_winner: bool
    hold_minutes: Decimal
    capture_ratio: Decimal
    pnl_r: Decimal
    execution_cost_r: Decimal
    closed_at: datetime
    entry_weekday: int  # 0=Mon .. 6=Sun
    entry_hour: int
    iv_bucket: str
    supporting_reason_count: int
    contradicting_reason_count: int
    entry_confidence: Decimal
    posttrade_thesis_verdict: Decimal
    size_multiplier: Decimal


@dataclass(frozen=True, slots=True)
class DecisionBiasInput:
    """Decision facts for recency (prior outcome → next size)."""

    decision_id: str
    created_at: datetime
    size_multiplier: Decimal | None
    prev_trade_pnl_r: Decimal | None


@dataclass(frozen=True, slots=True)
class WarmColdPair:
    """Warm vs cold review pair for anchoring (PART 9.4)."""

    trade_id: str
    warm_action: str
    cold_action: str


@dataclass(frozen=True, slots=True)
class BiasBatteryBands:
    """Optional breach thresholds. None disables that band check."""

    disposition_min: Decimal | None = Decimal("1.0")
    premature_exit_min: Decimal | None = Decimal("0.40")
    recency_abs_max: Decimal | None = Decimal("0.30")
    revenge_ratio_max: Decimal | None = Decimal("0.70")
    overconfidence_max: Decimal | None = Decimal("0.25")
    confirmation_max: Decimal | None = Decimal("4.0")
    anchoring_max: Decimal | None = Decimal("0.35")
    form_streak_size_lift_max: Decimal | None = Decimal("0.25")
    hindsight_corr_max: Decimal | None = Decimal("0.90")
    selection_max_share: Decimal | None = Decimal("0.45")
    cost_blindness_max: Decimal | None = Decimal("0.35")


def build_bias_report(
    *,
    as_of: datetime,
    cohort_id: str,
    trades: Sequence[TradeOutcomeInput],
    decisions: Sequence[DecisionBiasInput] = (),
    warm_cold_pairs: Sequence[WarmColdPair] = (),
    bands: BiasBatteryBands | None = None,
) -> BiasReport:
    """Compute all 11 PART 9.3 metrics for a fixture or live cohort."""
    bands = bands or BiasBatteryBands()
    builders = {
        BiasMetricId.DISPOSITION_EFFECT: lambda: _disposition(trades, bands),
        BiasMetricId.PREMATURE_EXIT: lambda: _premature_exit(trades, bands),
        BiasMetricId.RECENCY: lambda: _recency(decisions, bands),
        BiasMetricId.REVENGE: lambda: _revenge(trades, bands),
        BiasMetricId.OVERCONFIDENCE: lambda: _overconfidence(trades, bands),
        BiasMetricId.CONFIRMATION: lambda: _confirmation(trades, bands),
        BiasMetricId.ANCHORING: lambda: _anchoring(warm_cold_pairs, bands),
        BiasMetricId.FORM_STREAK: lambda: _form_streak(trades, bands),
        BiasMetricId.HINDSIGHT_DRIFT: lambda: _hindsight(trades, bands),
        BiasMetricId.SELECTION_DRIFT: lambda: _selection(trades, bands),
        BiasMetricId.COST_BLINDNESS: lambda: _cost_blindness(trades, bands),
    }
    metrics = tuple(builders[m]() for m in _ALL_METRICS)
    if len(metrics) != 11:
        raise RuntimeError(f"bias battery must emit 11 metrics, got {len(metrics)}")
    return BiasReport(
        as_of=as_of,
        cohort_id=cohort_id,
        metrics=metrics,
        attention_required=any(m.band_breached for m in metrics),
    )


def _disposition(
    trades: Sequence[TradeOutcomeInput], bands: BiasBatteryBands
) -> BiasMetricResult:
    winners = [t.hold_minutes for t in trades if t.is_winner]
    losers = [t.hold_minutes for t in trades if not t.is_winner]
    if not winners or not losers:
        return BiasMetricResult(
            metric_id=BiasMetricId.DISPOSITION_EFFECT,
            sample_size=len(trades),
            detail="need both winners and losers",
        )
    value = Decimal(str(median(winners))) / Decimal(str(median(losers)))
    return BiasMetricResult(
        metric_id=BiasMetricId.DISPOSITION_EFFECT,
        value=value.quantize(Decimal("0.0001")),
        sample_size=len(winners) + len(losers),
        band_breached=(
            bands.disposition_min is not None and value < bands.disposition_min
        ),
    )


def _premature_exit(
    trades: Sequence[TradeOutcomeInput], bands: BiasBatteryBands
) -> BiasMetricResult:
    winners = [t.capture_ratio for t in trades if t.is_winner]
    if not winners:
        return BiasMetricResult(
            metric_id=BiasMetricId.PREMATURE_EXIT,
            sample_size=0,
            detail="no winners",
        )
    value = sum(winners, Decimal(0)) / Decimal(len(winners))
    return BiasMetricResult(
        metric_id=BiasMetricId.PREMATURE_EXIT,
        value=value.quantize(Decimal("0.0001")),
        sample_size=len(winners),
        band_breached=(
            bands.premature_exit_min is not None and value < bands.premature_exit_min
        ),
    )


def _recency(
    decisions: Sequence[DecisionBiasInput], bands: BiasBatteryBands
) -> BiasMetricResult:
    pairs = [
        (d.prev_trade_pnl_r, d.size_multiplier)
        for d in decisions
        if d.prev_trade_pnl_r is not None and d.size_multiplier is not None
    ]
    if len(pairs) < 3:
        return BiasMetricResult(
            metric_id=BiasMetricId.RECENCY,
            sample_size=len(pairs),
            detail="need >=3 sized decisions with prior outcome",
        )
    value = _pearson([p[0] for p in pairs], [p[1] for p in pairs])
    return BiasMetricResult(
        metric_id=BiasMetricId.RECENCY,
        value=value.quantize(Decimal("0.0001")),
        sample_size=len(pairs),
        band_breached=(
            bands.recency_abs_max is not None and abs(value) > bands.recency_abs_max
        ),
    )


def _revenge(
    trades: Sequence[TradeOutcomeInput], bands: BiasBatteryBands
) -> BiasMetricResult:
    ordered = sorted(trades, key=lambda t: t.closed_at)
    after_loss: list[Decimal] = []
    after_win: list[Decimal] = []
    for i in range(1, len(ordered)):
        gap = Decimal(
            str(
                (ordered[i].closed_at - ordered[i - 1].closed_at).total_seconds() / 60.0
            )
        )
        if ordered[i - 1].is_winner:
            after_win.append(gap)
        else:
            after_loss.append(gap)
    if not after_loss or not after_win:
        return BiasMetricResult(
            metric_id=BiasMetricId.REVENGE,
            sample_size=len(ordered),
            detail="need re-entries after both wins and losses",
        )
    ratio = Decimal(str(median(after_loss))) / Decimal(str(median(after_win)))
    return BiasMetricResult(
        metric_id=BiasMetricId.REVENGE,
        value=ratio.quantize(Decimal("0.0001")),
        sample_size=len(after_loss) + len(after_win),
        # Concern when re-entry after loss is faster than after win (ratio < band).
        band_breached=(
            bands.revenge_ratio_max is not None and ratio < bands.revenge_ratio_max
        ),
    )


def _overconfidence(
    trades: Sequence[TradeOutcomeInput], bands: BiasBatteryBands
) -> BiasMetricResult:
    if len(trades) < 4:
        return BiasMetricResult(
            metric_id=BiasMetricId.OVERCONFIDENCE,
            sample_size=len(trades),
            detail="need >=4 trades",
        )
    buckets: dict[int, list[TradeOutcomeInput]] = defaultdict(list)
    for t in trades:
        buckets[min(int(t.entry_confidence * 5), 4)].append(t)
    reliability = Decimal(0)
    n = Decimal(len(trades))
    for group in buckets.values():
        freq = Decimal(sum(1 for t in group if t.is_winner)) / Decimal(len(group))
        conf = sum((t.entry_confidence for t in group), Decimal(0)) / Decimal(
            len(group)
        )
        reliability += (Decimal(len(group)) / n) * (conf - freq) ** 2
    return BiasMetricResult(
        metric_id=BiasMetricId.OVERCONFIDENCE,
        value=reliability.quantize(Decimal("0.0001")),
        sample_size=len(trades),
        band_breached=(
            bands.overconfidence_max is not None
            and reliability > bands.overconfidence_max
        ),
    )


def _confirmation(
    trades: Sequence[TradeOutcomeInput], bands: BiasBatteryBands
) -> BiasMetricResult:
    ratios = [
        Decimal(t.supporting_reason_count) / Decimal(t.contradicting_reason_count)
        for t in trades
        if t.contradicting_reason_count > 0
    ]
    if not ratios:
        return BiasMetricResult(
            metric_id=BiasMetricId.CONFIRMATION,
            sample_size=0,
            detail="no trades with contradicting reasons",
        )
    value = sum(ratios, Decimal(0)) / Decimal(len(ratios))
    return BiasMetricResult(
        metric_id=BiasMetricId.CONFIRMATION,
        value=value.quantize(Decimal("0.0001")),
        sample_size=len(ratios),
        band_breached=(
            bands.confirmation_max is not None and value > bands.confirmation_max
        ),
    )


def _anchoring(
    pairs: Sequence[WarmColdPair], bands: BiasBatteryBands
) -> BiasMetricResult:
    if not pairs:
        return BiasMetricResult(
            metric_id=BiasMetricId.ANCHORING,
            sample_size=0,
            detail="no warm/cold pairs",
        )
    disagree = sum(1 for p in pairs if p.warm_action != p.cold_action)
    value = Decimal(disagree) / Decimal(len(pairs))
    return BiasMetricResult(
        metric_id=BiasMetricId.ANCHORING,
        value=value.quantize(Decimal("0.0001")),
        sample_size=len(pairs),
        band_breached=(bands.anchoring_max is not None and value > bands.anchoring_max),
    )


def _form_streak(
    trades: Sequence[TradeOutcomeInput], bands: BiasBatteryBands
) -> BiasMetricResult:
    ordered = sorted(trades, key=lambda t: t.closed_at)
    if len(ordered) < 4:
        return BiasMetricResult(
            metric_id=BiasMetricId.FORM_STREAK,
            sample_size=len(ordered),
            detail="need >=4 trades",
        )
    baseline = sum((t.size_multiplier for t in ordered), Decimal(0)) / Decimal(
        len(ordered)
    )
    after: list[Decimal] = []
    streak = 0
    for t in ordered:
        if streak >= 3:
            after.append(t.size_multiplier)
        streak = streak + 1 if t.is_winner else 0
    if not after:
        return BiasMetricResult(
            metric_id=BiasMetricId.FORM_STREAK,
            sample_size=len(ordered),
            detail="no entries after 3-win streak",
        )
    lift = sum(after, Decimal(0)) / Decimal(len(after)) - baseline
    return BiasMetricResult(
        metric_id=BiasMetricId.FORM_STREAK,
        value=lift.quantize(Decimal("0.0001")),
        sample_size=len(after),
        band_breached=(
            bands.form_streak_size_lift_max is not None
            and lift > bands.form_streak_size_lift_max
        ),
        detail=f"baseline_size={baseline.quantize(Decimal('0.0001'))}",
    )


def _hindsight(
    trades: Sequence[TradeOutcomeInput], bands: BiasBatteryBands
) -> BiasMetricResult:
    if len(trades) < 3:
        return BiasMetricResult(
            metric_id=BiasMetricId.HINDSIGHT_DRIFT,
            sample_size=len(trades),
            detail="need >=3 trades",
        )
    value = _pearson(
        [t.entry_confidence for t in trades],
        [t.posttrade_thesis_verdict for t in trades],
    )
    return BiasMetricResult(
        metric_id=BiasMetricId.HINDSIGHT_DRIFT,
        value=value.quantize(Decimal("0.0001")),
        sample_size=len(trades),
        band_breached=(
            bands.hindsight_corr_max is not None and value > bands.hindsight_corr_max
        ),
    )


def _selection(
    trades: Sequence[TradeOutcomeInput], bands: BiasBatteryBands
) -> BiasMetricResult:
    if not trades:
        return BiasMetricResult(
            metric_id=BiasMetricId.SELECTION_DRIFT,
            sample_size=0,
            detail="empty cohort",
        )
    n = Decimal(len(trades))
    shares = []
    for keys in (
        [f"wd-{t.entry_weekday}" for t in trades],
        [f"hr-{t.entry_hour}" for t in trades],
        [f"iv-{t.iv_bucket}" for t in trades],
    ):
        counts = Counter(keys)
        shares.append(Decimal(max(counts.values())) / n)
    value = max(shares)
    return BiasMetricResult(
        metric_id=BiasMetricId.SELECTION_DRIFT,
        value=value.quantize(Decimal("0.0001")),
        sample_size=len(trades),
        band_breached=(
            bands.selection_max_share is not None and value > bands.selection_max_share
        ),
    )


def _cost_blindness(
    trades: Sequence[TradeOutcomeInput], bands: BiasBatteryBands
) -> BiasMetricResult:
    fracs = []
    for t in trades:
        gross = abs(t.pnl_r) + abs(t.execution_cost_r)
        if gross == 0:
            continue
        fracs.append(abs(t.execution_cost_r) / gross)
    if not fracs:
        return BiasMetricResult(
            metric_id=BiasMetricId.COST_BLINDNESS,
            sample_size=0,
            detail="no non-zero gross R",
        )
    value = sum(fracs, Decimal(0)) / Decimal(len(fracs))
    return BiasMetricResult(
        metric_id=BiasMetricId.COST_BLINDNESS,
        value=value.quantize(Decimal("0.0001")),
        sample_size=len(fracs),
        band_breached=(
            bands.cost_blindness_max is not None and value > bands.cost_blindness_max
        ),
    )


def _pearson(xs: Sequence[Decimal], ys: Sequence[Decimal]) -> Decimal:
    n = len(xs)
    if n < 2:
        return Decimal(0)
    mean_x = sum(xs, Decimal(0)) / Decimal(n)
    mean_y = sum(ys, Decimal(0)) / Decimal(n)
    num = sum(
        ((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True)),
        Decimal(0),
    )
    den_x = sum(((x - mean_x) ** 2 for x in xs), Decimal(0))
    den_y = sum(((y - mean_y) ** 2 for y in ys), Decimal(0))
    if den_x == 0 or den_y == 0:
        return Decimal(0)
    return num / (Decimal(str(sqrt(float(den_x)))) * Decimal(str(sqrt(float(den_y)))))
