"""Deterministic ExposureReport builder (ADESK-A4 / AGENT_DESK_SPEC PART 6).

Callers supply per-position Greeks and event tags; this module only aggregates.
Missing Greeks are treated as zero (fail-open on incomplete greeks would hide
risk — callers that cannot supply Greeks must refuse upstream).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from trading.domain.contracts.exposure import (
    CorrelationPair,
    EventOverlap,
    ExposureReport,
    NotionalBucket,
)
from trading.domain.contracts.portfolio import PortfolioSnapshot
from trading.domain.primitives import Money

__all__ = [
    "PositionExposureInput",
    "build_exposure_report",
]


@dataclass(frozen=True, slots=True)
class PositionExposureInput:
    """Per-open-position inputs the report cannot invent from broker truth alone."""

    trade_id: str
    underlying: str
    sector: str
    expiry: date | None
    # Index-equivalent Greeks for this position (signed).
    delta: Decimal
    vega: Decimal
    theta: Decimal
    gamma: Decimal
    notional: Money
    # +1 long risk, -1 short risk, 0 flat / undefined.
    directional_sign: int
    # Scheduled events / thesis tags this position references.
    event_ids: tuple[str, ...] = ()
    # Beta to NIFTY for this underlying (1.0 for NIFTY itself).
    beta_to_nifty: Decimal = Decimal(1)


def build_exposure_report(
    portfolio: PortfolioSnapshot,
    positions: Sequence[PositionExposureInput],
    *,
    beta_version: str,
    correlation_window: str,
    pairwise_correlations: Sequence[CorrelationPair] = (),
) -> ExposureReport:
    """Aggregate position inputs into a versioned ExposureReport."""
    equity = portfolio.exposure.equity
    currency = equity.currency

    net_delta = Decimal(0)
    net_vega = Decimal(0)
    net_theta = Decimal(0)
    net_gamma = Decimal(0)
    beta_weighted = Decimal(0)

    by_underlying: dict[str, Money] = {}
    by_sector: dict[str, Money] = {}
    by_expiry: dict[str, Money] = {}
    event_count: dict[str, int] = defaultdict(int)
    event_notional: dict[str, Money] = {}

    signed_risk = Decimal(0)
    abs_risk = Decimal(0)

    for row in positions:
        if row.notional.currency is not currency:
            raise ValueError(
                f"position {row.trade_id} notional currency "
                f"{row.notional.currency} != portfolio {currency}"
            )
        net_delta += row.delta
        net_vega += row.vega
        net_theta += row.theta
        net_gamma += row.gamma
        beta_weighted += row.delta * row.beta_to_nifty

        _add_money(by_underlying, row.underlying, row.notional)
        _add_money(by_sector, row.sector, row.notional)
        expiry_key = row.expiry.isoformat() if row.expiry is not None else "NONE"
        _add_money(by_expiry, expiry_key, row.notional)

        for event_id in row.event_ids:
            event_count[event_id] += 1
            _add_money(event_notional, event_id, row.notional)

        if row.directional_sign not in (-1, 0, 1):
            raise ValueError(
                f"directional_sign must be -1, 0, or 1; got {row.directional_sign}"
            )
        risk_weight = abs(row.notional.amount)
        signed_risk += Decimal(row.directional_sign) * risk_weight
        abs_risk += risk_weight

    agreement = (
        Decimal(0)
        if abs_risk == 0
        else (abs(signed_risk) / abs_risk).quantize(Decimal("0.000001"))
    )

    return ExposureReport(
        as_of=portfolio.as_of,
        beta_version=beta_version,
        correlation_window=correlation_window,
        open_position_count=len(positions),
        equity=equity,
        net_delta=net_delta,
        net_vega=net_vega,
        net_theta=net_theta,
        net_gamma=net_gamma,
        beta_weighted_delta_nifty=beta_weighted,
        notional_by_underlying=_buckets(by_underlying),
        notional_by_sector=_buckets(by_sector),
        notional_by_expiry=_buckets(by_expiry),
        pairwise_correlations=tuple(pairwise_correlations),
        event_overlaps=tuple(
            EventOverlap(
                event_id=event_id,
                position_count=event_count[event_id],
                notional=event_notional[event_id],
            )
            for event_id in sorted(event_count)
        ),
        directional_agreement_ratio=agreement,
    )


def _add_money(bucket: dict[str, Money], key: str, amount: Money) -> None:
    current = bucket.get(key)
    bucket[key] = amount if current is None else current + amount


def _buckets(raw: Mapping[str, Money]) -> tuple[NotionalBucket, ...]:
    return tuple(
        NotionalBucket(key=key, notional=raw[key]) for key in sorted(raw)
    )

