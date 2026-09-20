"""ADESK-A4: ExposureReport builder + PART 6 hard-limit matrix."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from tests import factories as f
from tests.test_risk_gateway import ROOT, _gateway_request
from trading.broker.paper import PaperBroker
from trading.config import load_config, load_risk_policy
from trading.domain.clock import FrozenClock
from trading.domain.contracts.exposure import CorrelationPair
from trading.domain.enums import ReasonCode, RiskAction
from trading.domain.ids import SequentialIdFactory
from trading.portfolio.exposure import PositionExposureInput, build_exposure_report
from trading.risk import CapitalReservationService, RiskGateway
from trading.risk.limits import evaluate_exposure_limits
from trading.storage import TradingStore

POLICY = load_risk_policy(ROOT / "config" / "risk.yaml").config


def _pos(
    trade_id: str,
    *,
    underlying: str = "NIFTY",
    sector: str = "INDEX",
    expiry: date | None = date(2026, 9, 24),
    delta: str = "0",
    vega: str = "0",
    theta: str = "0",
    gamma: str = "0",
    notional: str = "10000",
    directional_sign: int = 1,
    event_ids: tuple[str, ...] = (),
    beta: str = "1",
) -> PositionExposureInput:
    return PositionExposureInput(
        trade_id=trade_id,
        underlying=underlying,
        sector=sector,
        expiry=expiry,
        delta=Decimal(delta),
        vega=Decimal(vega),
        theta=Decimal(theta),
        gamma=Decimal(gamma),
        notional=f.money(notional),
        directional_sign=directional_sign,
        event_ids=event_ids,
        beta_to_nifty=Decimal(beta),
    )


def test_build_exposure_report_aggregates_greeks_and_buckets() -> None:
    portfolio = f.portfolio_snapshot()
    report = build_exposure_report(
        portfolio,
        (
            _pos("t1", delta="10", vega="100", notional="50000", directional_sign=1),
            _pos(
                "t2",
                underlying="BANKNIFTY",
                sector="BANK",
                expiry=date(2026, 10, 1),
                delta="-4",
                vega="50",
                notional="30000",
                directional_sign=-1,
                event_ids=("RBI",),
                beta="1.2",
            ),
        ),
        beta_version="beta-v1",
        correlation_window="60d",
        pairwise_correlations=(
            CorrelationPair(
                underlying_a="BANKNIFTY",
                underlying_b="NIFTY",
                correlation=Decimal("0.85"),
                window="60d",
            ),
        ),
    )
    assert report.net_delta == Decimal("6")
    assert report.net_vega == Decimal("150")
    assert report.beta_weighted_delta_nifty == Decimal("10") + Decimal("-4") * Decimal(
        "1.2"
    )
    assert report.open_position_count == 2
    assert {b.key for b in report.notional_by_underlying} == {"BANKNIFTY", "NIFTY"}
    assert {b.key for b in report.notional_by_expiry} == {"2026-09-24", "2026-10-01"}
    assert report.event_overlaps[0].event_id == "RBI"
    assert report.event_overlaps[0].position_count == 1
    # |50k - 30k| / 80k = 0.25
    assert report.directional_agreement_ratio == Decimal("0.250000")
    assert report.beta_version == "beta-v1"


def test_empty_book_agreement_is_zero() -> None:
    report = build_exposure_report(
        f.portfolio_snapshot(),
        (),
        beta_version="b",
        correlation_window="60d",
    )
    assert report.directional_agreement_ratio == Decimal(0)
    assert report.net_delta == Decimal(0)


def test_exposure_limits_pass_on_benign_book() -> None:
    # Balanced signs keep agreement under directional_agreement_max (0.85).
    report = build_exposure_report(
        f.portfolio_snapshot(),
        (
            _pos("t1", vega="10", notional="10000", directional_sign=1),
            _pos("t2", vega="10", notional="10000", directional_sign=-1),
        ),
        beta_version="b",
        correlation_window="60d",
    )
    result = evaluate_exposure_limits(report, POLICY)
    assert result.passed
    assert result.reason_codes == (ReasonCode.OK,)


@pytest.mark.parametrize(
    ("positions", "limit_name"),
    [
        (
            (_pos("t1", vega="6000", notional="10000"),),
            "net_vega_limit",
        ),
        (
            (
                _pos("t1", notional="200000", expiry=date(2026, 9, 24)),
                _pos("t2", notional="100000", expiry=date(2026, 9, 24)),
            ),
            "expiry_day_notional_fraction",
        ),
        (
            (
                _pos("t1", notional="100000", directional_sign=1),
                _pos("t2", notional="100000", directional_sign=1),
                _pos("t3", notional="10000", directional_sign=-1),
            ),
            "directional_agreement_max",
        ),
        (
            (
                _pos("t1", notional="100000", event_ids=("CPI",)),
                _pos("t2", notional="100000", event_ids=("CPI",)),
            ),
            "single_event_exposure_fraction",
        ),
        (
            (_pos("t1", delta="600", notional="10000"),),
            "net_delta_limit",
        ),
    ],
    ids=(
        "vega",
        "expiry-day",
        "directional-agreement",
        "single-event",
        "net-delta",
    ),
)
def test_exposure_limit_matrix(
    positions: tuple[PositionExposureInput, ...],
    limit_name: str,
) -> None:
    report = build_exposure_report(
        f.portfolio_snapshot(),
        positions,
        beta_version="b",
        correlation_window="60d",
    )
    result = evaluate_exposure_limits(report, POLICY)
    assert result.passed is False
    assert limit_name in result.applied_limits
    assert ReasonCode.OK not in result.reason_codes


def test_gateway_rejects_on_exposure_vega_limit(tmp_path: Path) -> None:
    clock = FrozenClock(f.NOW)
    ids = SequentialIdFactory(clock.instant)
    store = TradingStore.open(tmp_path / "trading.db", clock=clock)
    broker = PaperBroker.from_fixtures(
        ROOT / "tests" / "fixtures" / "broker",
        clock=clock,
        id_factory=ids,
    )
    gateway = RiskGateway(
        account_config=load_config(ROOT / "config" / "base.yaml"),
        risk_policy=load_risk_policy(ROOT / "config" / "risk.yaml"),
        reservation_service=CapitalReservationService(
            store, clock=clock, id_factory=ids
        ),
        margin_preview=broker,
        clock=clock,
        id_factory=ids,
    )
    report = build_exposure_report(
        f.portfolio_snapshot(),
        (_pos("t1", vega="9000", notional="10000"),),
        beta_version="b",
        correlation_window="60d",
    )
    request = replace(_gateway_request(), exposure_report=report)
    decision = gateway.evaluate(request)
    assert decision.action is RiskAction.REJECT
    assert "net_vega_limit" in decision.applied_limits
