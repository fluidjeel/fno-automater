"""Per-mode forecast calibration against realized horizon labels.

Answers one question per mode: are its probabilities better than the naive
base rate and than what option prices already imply? Refit output is a
proposal only; coefficients change through the normal config promotion path.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal

from trading.domain.contracts.forecast import (
    ForecastBarrier,
    ForecastLabel,
    ModeForecast,
)
from trading.domain.enums import ModeId
from trading.forecast.logistic import fit_logistic, logistic_probability

__all__ = [
    "CalibrationBin",
    "ConvictionBucket",
    "EdgeSummary",
    "ModeCalibration",
    "ProbabilityScore",
    "RefitProposal",
    "calibrate_modes",
    "format_calibration_report",
]

_ZERO = Decimal(0)
_ONE = Decimal(1)
_HALF = Decimal("0.5")
_EPSILON = Decimal("1e-9")
_QUANT = Decimal("0.0001")
_BIN_COUNT = 10
_CONVICTION_EDGES = (Decimal("0.05"), Decimal("0.10"), Decimal("0.20"))


def _q(value: Decimal) -> Decimal:
    return value.quantize(_QUANT, rounding=ROUND_HALF_EVEN)


def _mean(values: Sequence[Decimal]) -> Decimal | None:
    return _q(sum(values, _ZERO) / len(values)) if values else None


@dataclass(frozen=True, slots=True)
class ProbabilityScore:
    """Proper scores for one probability stream and its baselines."""

    samples: int
    brier: Decimal
    log_loss: Decimal
    base_rate: Decimal
    brier_base_rate: Decimal
    brier_implied: Decimal | None
    implied_samples: int

    @property
    def skill_vs_base(self) -> Decimal:
        """Brier skill score: 1 is perfect, 0 equals the base rate, <0 is worse."""
        if self.brier_base_rate == 0:
            return _ZERO
        return _q(_ONE - self.brier / self.brier_base_rate)


@dataclass(frozen=True, slots=True)
class CalibrationBin:
    """One reliability bucket: how often events at this stated odds happened."""

    lower: Decimal
    upper: Decimal
    samples: int
    mean_predicted: Decimal
    observed: Decimal


@dataclass(frozen=True, slots=True)
class ConvictionBucket:
    """Directional hit rate for forecasts whose |p_up - 0.5| falls in range."""

    lower: Decimal
    upper: Decimal | None
    samples: int
    hit_rate: Decimal


@dataclass(frozen=True, slots=True)
class EdgeSummary:
    """Predicted versus realized structure edge for priced forecasts."""

    samples: int
    mean_predicted: Decimal | None
    mean_realized: Decimal | None
    positive_predicted: int
    mean_realized_when_positive: Decimal | None


@dataclass(frozen=True, slots=True)
class RefitProposal:
    """Chronological-holdout refit; adopt only if holdout loss improves."""

    train_samples: int
    holdout_samples: int
    intercept: Decimal
    coefficients: dict[str, Decimal]
    holdout_log_loss_current: Decimal
    holdout_log_loss_refit: Decimal

    @property
    def improves(self) -> bool:
        return self.holdout_log_loss_refit < self.holdout_log_loss_current


@dataclass(frozen=True, slots=True)
class ModeCalibration:
    """Everything the report says about one mode."""

    mode_id: ModeId
    labelled: int
    incomplete: int
    direction: ProbabilityScore | None
    event: ProbabilityScore | None
    reliability: tuple[CalibrationBin, ...]
    conviction: tuple[ConvictionBucket, ...]
    barriers: dict[ForecastBarrier, int]
    edge: EdgeSummary
    refit: RefitProposal | None


def _clip(p: Decimal) -> Decimal:
    return min(max(p, _EPSILON), _ONE - _EPSILON)


def _log_loss(pairs: Sequence[tuple[Decimal, bool]]) -> Decimal:
    total = _ZERO
    for p, outcome in pairs:
        clipped = _clip(p)
        total -= clipped.ln() if outcome else (_ONE - clipped).ln()
    return _q(total / len(pairs))


def _brier(pairs: Sequence[tuple[Decimal, bool]]) -> Decimal:
    return _q(
        sum(((p - (_ONE if hit else _ZERO)) ** 2 for p, hit in pairs), _ZERO)
        / len(pairs)
    )


def _score(
    pairs: Sequence[tuple[Decimal, bool]],
    implied: Sequence[tuple[Decimal, bool]] = (),
) -> ProbabilityScore | None:
    if not pairs:
        return None
    base = Decimal(sum(hit for _p, hit in pairs)) / len(pairs)
    return ProbabilityScore(
        samples=len(pairs),
        brier=_brier(pairs),
        log_loss=_log_loss(pairs),
        base_rate=_q(base),
        brier_base_rate=_brier([(base, hit) for _p, hit in pairs]),
        brier_implied=_brier(implied) if implied else None,
        implied_samples=len(implied),
    )


def _reliability(pairs: Sequence[tuple[Decimal, bool]]) -> tuple[CalibrationBin, ...]:
    width = _ONE / _BIN_COUNT
    bins: list[CalibrationBin] = []
    for index in range(_BIN_COUNT):
        lower = width * index
        upper = width * (index + 1)
        members = [
            (p, hit)
            for p, hit in pairs
            if lower <= p < upper or (index == _BIN_COUNT - 1 and p == _ONE)
        ]
        if not members:
            continue
        bins.append(
            CalibrationBin(
                lower=_q(lower),
                upper=_q(upper),
                samples=len(members),
                mean_predicted=_q(sum((p for p, _h in members), _ZERO) / len(members)),
                observed=_q(Decimal(sum(h for _p, h in members)) / len(members)),
            )
        )
    return tuple(bins)


def _conviction(
    rows: Sequence[tuple[ModeForecast, ForecastLabel]],
) -> tuple[ConvictionBucket, ...]:
    lowers = (_ZERO, *_CONVICTION_EDGES)
    uppers: tuple[Decimal | None, ...] = (*_CONVICTION_EDGES, None)
    buckets: list[ConvictionBucket] = []
    for lower, upper in zip(lowers, uppers, strict=True):
        hits = [
            (forecast.direction * label.realized_return) > 0
            for forecast, label in rows
            if forecast.direction != 0
            and lower <= abs(forecast.p_up - _HALF)
            and (upper is None or abs(forecast.p_up - _HALF) < upper)
        ]
        if hits:
            buckets.append(
                ConvictionBucket(
                    lower=lower,
                    upper=upper,
                    samples=len(hits),
                    hit_rate=_q(Decimal(sum(hits)) / len(hits)),
                )
            )
    return tuple(buckets)


def _edge(rows: Sequence[tuple[ModeForecast, ForecastLabel]]) -> EdgeSummary:
    priced: list[tuple[Decimal, Decimal]] = []
    for forecast, label in rows:
        if forecast.edge_after_costs is not None and label.realized_edge is not None:
            priced.append((forecast.edge_after_costs, label.realized_edge))
    predicted = [p for p, _r in priced]
    realized = [r for _p, r in priced]
    positive = [r for p, r in priced if p > 0]
    return EdgeSummary(
        samples=len(priced),
        mean_predicted=_mean(predicted),
        mean_realized=_mean(realized),
        positive_predicted=len(positive),
        mean_realized_when_positive=_mean(positive),
    )


def _refit(
    rows: Sequence[tuple[ModeForecast, ForecastLabel]],
    *,
    current: tuple[Decimal, Mapping[str, Decimal]] | None,
    holdout_fraction: Decimal,
    min_samples: int,
) -> RefitProposal | None:
    """Fit on the earlier slice, score on the later one; never shuffle time."""
    if current is None or len(rows) < min_samples:
        return None
    ordered = sorted(rows, key=lambda row: row[0].as_of)
    cut = int(Decimal(len(ordered)) * (_ONE - holdout_fraction))
    train, holdout = ordered[:cut], ordered[cut:]
    if not train or not holdout:
        return None
    intercept, coefficients = current
    names = tuple(coefficients)
    samples = [
        (forecast.features, label.realized_return > 0) for forecast, label in train
    ]
    fit = fit_logistic(samples, names)

    def _holdout_loss(b: Decimal, w: Mapping[str, Decimal]) -> Decimal:
        pairs = [
            (
                logistic_probability(
                    intercept=b, coefficients=w, features=forecast.features
                )[0],
                label.realized_return > 0,
            )
            for forecast, label in holdout
        ]
        return _log_loss(pairs)

    return RefitProposal(
        train_samples=len(train),
        holdout_samples=len(holdout),
        intercept=fit.intercept,
        coefficients=fit.coefficients,
        holdout_log_loss_current=_holdout_loss(intercept, coefficients),
        holdout_log_loss_refit=_holdout_loss(fit.intercept, fit.coefficients),
    )


def _calibrate_mode(
    mode_id: ModeId,
    rows: Sequence[tuple[ModeForecast, ForecastLabel]],
    incomplete: int,
    *,
    current: tuple[Decimal, Mapping[str, Decimal]] | None,
    holdout_fraction: Decimal,
    min_refit_samples: int,
) -> ModeCalibration:
    # Mode-level rows (no family) are one per cadence tick; family rows repeat the
    # same view per bound structure and would overweight busy cycles.
    views = [row for row in rows if row[0].family_id is None] or list(rows)
    direction = [
        (forecast.p_up, label.realized_return > 0) for forecast, label in views
    ]
    event = [(forecast.p_event, label.event_occurred) for forecast, label in views]
    implied: list[tuple[Decimal, bool]] = []
    for forecast, label in views:
        if forecast.p_event_implied is not None:
            implied.append((forecast.p_event_implied, label.event_occurred))
    barriers: dict[ForecastBarrier, int] = {}
    for _forecast, label in views:
        barriers[label.barrier] = barriers.get(label.barrier, 0) + 1
    return ModeCalibration(
        mode_id=mode_id,
        labelled=len(views),
        incomplete=incomplete,
        direction=_score(direction),
        event=_score(event, implied),
        reliability=_reliability(event),
        conviction=_conviction(views),
        barriers=barriers,
        edge=_edge([row for row in rows if row[0].family_id is not None]),
        refit=_refit(
            views,
            current=current,
            holdout_fraction=holdout_fraction,
            min_samples=min_refit_samples,
        ),
    )


def calibrate_modes(
    forecasts: Iterable[ModeForecast],
    labels: Iterable[ForecastLabel],
    *,
    current_models: Mapping[ModeId, tuple[Decimal, Mapping[str, Decimal]]]
    | None = None,
    holdout_fraction: Decimal = Decimal("0.3"),
    min_refit_samples: int = 200,
) -> tuple[ModeCalibration, ...]:
    """Join forecasts to complete labels and score each mode independently."""
    by_id = {forecast.forecast_id: forecast for forecast in forecasts}
    grouped: dict[ModeId, list[tuple[ModeForecast, ForecastLabel]]] = {}
    incomplete: dict[ModeId, int] = {}
    for label in labels:
        forecast = by_id.get(label.forecast_id)
        if forecast is None:
            continue
        if not label.complete:
            incomplete[forecast.mode_id] = incomplete.get(forecast.mode_id, 0) + 1
            continue
        grouped.setdefault(forecast.mode_id, []).append((forecast, label))
    return tuple(
        _calibrate_mode(
            mode_id,
            grouped.get(mode_id, []),
            incomplete.get(mode_id, 0),
            current=(current_models or {}).get(mode_id),
            holdout_fraction=holdout_fraction,
            min_refit_samples=min_refit_samples,
        )
        for mode_id in ModeId
        if mode_id in grouped or mode_id in incomplete
    )


def _score_lines(name: str, score: ProbabilityScore | None) -> list[str]:
    if score is None:
        return [f"  {name}: no labelled samples"]
    implied = (
        "n/a"
        if score.brier_implied is None
        else f"{score.brier_implied} (n={score.implied_samples})"
    )
    return [
        f"  {name}: n={score.samples} brier={score.brier} log_loss={score.log_loss}",
        f"    base_rate={score.base_rate} brier_base={score.brier_base_rate} "
        f"skill_vs_base={score.skill_vs_base} brier_implied={implied}",
    ]


def format_calibration_report(results: Sequence[ModeCalibration]) -> str:
    """Plain-text report, one block per mode."""
    if not results:
        return "no labelled forecasts"
    lines: list[str] = []
    for result in results:
        lines.append(
            f"== {result.mode_id.value}: labelled={result.labelled} "
            f"incomplete={result.incomplete}"
        )
        lines.extend(_score_lines("direction p_up", result.direction))
        lines.extend(_score_lines("event p_event", result.event))
        if result.reliability:
            lines.append("  reliability (predicted -> observed):")
            lines.extend(
                f"    [{item.lower},{item.upper}) n={item.samples} "
                f"{item.mean_predicted} -> {item.observed}"
                for item in result.reliability
            )
        if result.conviction:
            lines.append("  hit rate by |p_up-0.5|:")
            lines.extend(
                f"    >= {item.lower}"
                + ("" if item.upper is None else f" < {item.upper}")
                + f": n={item.samples} hit={item.hit_rate}"
                for item in result.conviction
            )
        barriers = ", ".join(
            f"{key.value}={value}" for key, value in sorted(result.barriers.items())
        )
        lines.append(f"  barriers: {barriers or 'none'}")
        edge = result.edge
        lines.append(
            f"  edge: n={edge.samples} predicted={edge.mean_predicted} "
            f"realized={edge.mean_realized} positive_n={edge.positive_predicted} "
            f"realized_when_positive={edge.mean_realized_when_positive}"
        )
        refit = result.refit
        if refit is None:
            lines.append("  refit: insufficient samples")
        else:
            verdict = "PROPOSE" if refit.improves else "KEEP CURRENT"
            lines.append(
                f"  refit ({verdict}): train={refit.train_samples} "
                f"holdout={refit.holdout_samples} "
                f"loss {refit.holdout_log_loss_current} -> "
                f"{refit.holdout_log_loss_refit}"
            )
            lines.append(f"    intercept: {refit.intercept}")
            lines.extend(
                f"    {name}: {value}"
                for name, value in sorted(refit.coefficients.items())
            )
    return "\n".join(lines)
