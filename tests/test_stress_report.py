"""ADESK-A5: StressReport builder + assume_no_fills + entry freeze on breach."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from trading.analytics.stress import (
    PositionStressInput,
    build_stress_entry_freeze,
    build_stress_report,
    evaluate_tail_budget,
)
from trading.config import load_risk_policy
from trading.domain.contracts.stress import FragilityFlag, StressScenarioId
from trading.domain.enums import ReasonCode
from trading.domain.primitives import Currency, Money

ROOT = Path(__file__).resolve().parents[1]
POLICY = load_risk_policy(ROOT / "config" / "risk.yaml").config
NOW = datetime(2026, 9, 20, 15, 30, tzinfo=UTC)
INR = Currency.INR


def _money(v: str) -> Money:
    return Money.of(v, INR)


def _debit(
    *, max_loss: str = "25000", delta: str = "0", vega: str = "0"
) -> PositionStressInput:
    return PositionStressInput(
        trade_id="debit-1",
        defined_risk_max_loss=_money(max_loss),
        delta_pnl_per_pct=_money(delta),
        vega_pnl_per_pct=_money(vega),
        is_defined_risk=True,
    )


def test_debit_spread_assume_no_fills_equals_net_debit() -> None:
    report = build_stress_report(
        as_of=NOW,
        snapshot_id="s1",
        equity=_money("1000000"),
        positions=(_debit(max_loss="25000"),),
        tail_budget_fraction=POLICY.tail_budget_fraction,
    )
    no_fills = [r for r in report.results if r.assume_no_fills]
    assert no_fills
    for row in no_fills:
        assert row.pnl == _money("-25000")
    assert report.worst_case == _money("-25000")
    assert FragilityFlag.DEFINED_RISK_ONLY in report.fragility_flags
    assert FragilityFlag.WORST_CASE_IS_NO_FILLS in report.fragility_flags
    assert report.breached_budget is False


def test_tail_budget_breach_builds_entry_freeze() -> None:
    report = build_stress_report(
        as_of=NOW,
        snapshot_id="s2",
        equity=_money("100000"),  # 25k loss = 25% > 8%
        positions=(_debit(max_loss="25000"),),
        tail_budget_fraction=POLICY.tail_budget_fraction,
    )
    assert report.breached_budget is True
    assert evaluate_tail_budget(report) is True
    assert FragilityFlag.TAIL_BUDGET_BREACHED in report.fragility_flags
    freeze = build_stress_entry_freeze(report, now=NOW)
    assert freeze is not None
    assert freeze.entries_blocked is True
    assert freeze.reason_code is ReasonCode.ENTRY_FROZEN


def test_no_freeze_when_inside_budget() -> None:
    report = build_stress_report(
        as_of=NOW,
        snapshot_id="s3",
        equity=_money("1000000"),
        positions=(_debit(max_loss="10000"),),
        tail_budget_fraction=POLICY.tail_budget_fraction,
    )
    assert report.breached_budget is False
    assert build_stress_entry_freeze(report, now=NOW) is None


def test_defined_risk_clamps_shock_to_max_loss() -> None:
    # Huge adverse delta would exceed debit; clamp to -max_loss.
    report = build_stress_report(
        as_of=NOW,
        snapshot_id="s4",
        equity=_money("1000000"),
        positions=(_debit(max_loss="5000", delta="5000"),),  # -8% * 5000 = -40k
        tail_budget_fraction=Decimal("0.50"),
        scenarios=(
            __import__(
                "trading.domain.contracts.stress", fromlist=["StressScenario"]
            ).StressScenario(
                scenario_id=StressScenarioId.GAP_DOWN_8,
                spot_shock_pct=Decimal("-8"),
                iv_shock_pct=Decimal("0"),
                assume_no_fills=False,
            ),
        ),
    )
    assert len(report.results) == 1
    assert report.results[0].pnl == _money("-5000")


def test_policy_loads_tail_budget_fraction() -> None:
    assert POLICY.tail_budget_fraction == Decimal("0.08")
    assert POLICY.policy_version == "6"
