"""Deterministic logistic scoring and an offline Decimal fitter."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

__all__ = ["LogisticFit", "fit_logistic", "logistic_probability", "sigmoid"]

_ZERO = Decimal(0)
_ONE = Decimal(1)
_EXP_CAP = Decimal(40)


def sigmoid(x: Decimal) -> Decimal:
    """Numerically bounded logistic function."""
    clamped = min(max(x, -_EXP_CAP), _EXP_CAP)
    return _ONE / (_ONE + (-clamped).exp())


def logistic_probability(
    *,
    intercept: Decimal,
    coefficients: Mapping[str, Decimal],
    features: Mapping[str, Decimal],
) -> tuple[Decimal, tuple[str, ...]]:
    """Return P(y=1) and the coefficient names whose feature was absent.

    An absent feature contributes zero rather than an invented value; the
    caller records it so calibration can see the model ran degraded.
    """
    score = intercept
    absent: list[str] = []
    for name in sorted(coefficients):
        value = features.get(name)
        if value is None:
            absent.append(name)
            continue
        score += coefficients[name] * value
    return sigmoid(score), tuple(absent)


@dataclass(frozen=True, slots=True)
class LogisticFit:
    """Fitted coefficients plus the training log loss."""

    intercept: Decimal
    coefficients: dict[str, Decimal]
    log_loss: Decimal
    samples: int


def fit_logistic(
    rows: Sequence[tuple[Mapping[str, Decimal], bool]],
    names: Sequence[str],
    *,
    l2: Decimal = Decimal("0.01"),
    learning_rate: Decimal = Decimal("0.5"),
    iterations: int = 300,
) -> LogisticFit:
    """L2-regularized batch gradient descent; missing features count as zero."""
    if not rows:
        raise ValueError("cannot fit a logistic model on zero rows")
    ordered = tuple(sorted(names))
    weights = {name: _ZERO for name in ordered}
    intercept = _ZERO
    count = Decimal(len(rows))
    for _ in range(iterations):
        grad_b = _ZERO
        grad = {name: _ZERO for name in ordered}
        for features, label in rows:
            score = intercept + sum(
                (weights[name] * features.get(name, _ZERO) for name in ordered),
                _ZERO,
            )
            error = sigmoid(score) - (_ONE if label else _ZERO)
            grad_b += error
            for name in ordered:
                grad[name] += error * features.get(name, _ZERO)
        intercept -= learning_rate * grad_b / count
        for name in ordered:
            weights[name] -= learning_rate * (grad[name] / count + l2 * weights[name])
    loss = _ZERO
    epsilon = Decimal("1e-9")
    for features, label in rows:
        p = sigmoid(
            intercept
            + sum((weights[n] * features.get(n, _ZERO) for n in ordered), _ZERO)
        )
        p = min(max(p, epsilon), _ONE - epsilon)
        loss -= p.ln() if label else (_ONE - p).ln()
    quant = Decimal("0.000001")
    return LogisticFit(
        intercept=intercept.quantize(quant),
        coefficients={name: value.quantize(quant) for name, value in weights.items()},
        log_loss=(loss / count).quantize(quant),
        samples=len(rows),
    )
