"""ADESK-D2: FRAGILITY + POSTTRADE promote to ADVISORY via signed grant."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from trading.ai.fragility import (
    FRAGILITY_MODEL_ID,
    FRAGILITY_POLICY_VERSION,
    FRAGILITY_PROMPT_VERSION,
    build_fragility_telegram_line,
    maybe_log_fragility,
)
from trading.ai.posttrade import (
    POSTTRADE_MODEL_ID,
    POSTTRADE_POLICY_VERSION,
    POSTTRADE_PROMPT_VERSION,
    build_posttrade_advisory_line,
    build_trade_attribution,
    maybe_log_posttrade,
)
from trading.analytics.stress import PositionStressInput, build_stress_report
from trading.domain.contracts.authority import AuthorityGrant
from trading.domain.contracts.posttrade import entry_journal_hash
from trading.domain.contracts.stress import StressReport
from trading.domain.enums import (
    AgentAction,
    AuthorityMode,
    DeskRole,
    Environment,
)
from trading.domain.primitives import Currency, Money

NOW = datetime(2026, 9, 20, 15, 0, tzinfo=UTC)
INR = Currency.INR


def _advisory_grant(
    *, role: DeskRole, model: str, prompt: str, policy: str
) -> AuthorityGrant:
    return AuthorityGrant.issue(
        grant_id=f"GRANT-{role.value}-ADV",
        role=role,
        mode=AuthorityMode.ADVISORY,
        allowed_actions=(
            AgentAction.REQUEST_OPERATOR_ATTENTION,
            AgentAction.HOLD,
            AgentAction.RECORD_IMPROVEMENT,
        ),
        strategy_families=("positional_long_option",),
        environment=Environment.PAPER,
        policy_version=policy,
        prompt_version=prompt,
        model_id=model,
        evidence_report_id="SCORECARD-D2",
        granted_at=NOW - timedelta(hours=1),
        valid_until=NOW + timedelta(days=30),
        signed_by="operator@desk",
    )


def _stress() -> StressReport:
    return build_stress_report(
        as_of=NOW,
        snapshot_id="stress-d2",
        equity=Money.of("1000000", INR),
        positions=(
            PositionStressInput(
                trade_id="t1",
                defined_risk_max_loss=Money.of("25000", INR),
                delta_pnl_per_pct=Money.of("0", INR),
                vega_pnl_per_pct=Money.of("0", INR),
                is_defined_risk=True,
            ),
        ),
        tail_budget_fraction=Decimal("0.08"),
    )


def test_fragility_advisory_with_valid_grant() -> None:
    report = _stress()
    grant = _advisory_grant(
        role=DeskRole.FRAGILITY,
        model=FRAGILITY_MODEL_ID,
        prompt=FRAGILITY_PROMPT_VERSION,
        policy=FRAGILITY_POLICY_VERSION,
    )
    result = maybe_log_fragility(
        report, decision_log=None, enabled=True, run_id="r1", grant=grant, now=NOW
    )
    assert result.mode is AuthorityMode.ADVISORY
    assert result.status == "LOGGED"
    assert result.decision is not None
    assert result.decision.mode is AuthorityMode.ADVISORY
    assert result.narration is not None
    assert "FRAGILITY stress-d2" in result.narration.telegram_line
    # Builder path reaches advisory shape without sending.
    built = build_fragility_telegram_line(report)
    assert built.telegram_line == result.narration.telegram_line


def test_fragility_demotes_to_observe_without_grant() -> None:
    report = _stress()
    result = maybe_log_fragility(
        report, decision_log=None, enabled=True, run_id="r2", grant=None, now=NOW
    )
    assert result.mode is AuthorityMode.OBSERVE
    assert result.status == "OBSERVE"
    assert result.decision is not None
    assert result.decision.mode is AuthorityMode.OBSERVE
    # Narration still built; advisory delivery gated by mode.
    assert result.narration is not None


def test_posttrade_advisory_with_valid_grant() -> None:
    h = entry_journal_hash(thesis_hash="abc", narrative="entry note")
    attr = build_trade_attribution(
        trade_id="t1",
        thesis_id="th1",
        thesis_hash="abc",
        entry_narrative="entry note",
        stored_entry_hash=h,
        outcome_r=Decimal("1.2"),
        mae_r=Decimal("0.3"),
        mfe_r=Decimal("1.5"),
        thesis_correct=True,
    )
    grant = _advisory_grant(
        role=DeskRole.POSTTRADE,
        model=POSTTRADE_MODEL_ID,
        prompt=POSTTRADE_PROMPT_VERSION,
        policy=POSTTRADE_POLICY_VERSION,
    )
    result = maybe_log_posttrade(
        as_of=NOW,
        attribution=attr,
        decision_log=None,
        enabled=True,
        run_id="r3",
        snapshot_id="snap-1",
        grant=grant,
        now=NOW,
    )
    assert result.mode is AuthorityMode.ADVISORY
    assert result.status == "LOGGED"
    assert result.decision is not None
    assert result.decision.mode is AuthorityMode.ADVISORY
    assert result.advisory is not None
    assert "POSTTRADE t1" in result.advisory.line
    assert build_posttrade_advisory_line(attr).line == result.advisory.line


def test_posttrade_demotes_to_observe_without_grant() -> None:
    h = entry_journal_hash(thesis_hash="abc", narrative="entry note")
    attr = build_trade_attribution(
        trade_id="t2",
        thesis_id="th2",
        thesis_hash="abc",
        entry_narrative="entry note",
        stored_entry_hash=h,
        outcome_r=Decimal("-0.5"),
        mae_r=Decimal("0.5"),
        mfe_r=Decimal("0.1"),
        thesis_correct=False,
    )
    result = maybe_log_posttrade(
        as_of=NOW,
        attribution=attr,
        decision_log=None,
        enabled=True,
        run_id="r4",
        snapshot_id="snap-2",
        grant=None,
        now=NOW,
    )
    assert result.mode is AuthorityMode.OBSERVE
    assert result.status == "OBSERVE"
    assert result.decision is not None
    assert result.decision.mode is AuthorityMode.OBSERVE
