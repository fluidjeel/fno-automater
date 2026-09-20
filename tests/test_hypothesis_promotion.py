"""ADESK-E2: hypothesis → SHADOW experiment via existing promotion ladder."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from tests.factories import experiment
from trading.analytics.hypothesis_promotion import (
    cluster_to_hypothesis,
    hypothesis_to_shadow_experiment,
    promote_improvements,
)
from trading.analytics.improvements import normalize_claim_key
from trading.domain.contracts.evaluation import ExperimentDefinition
from trading.domain.contracts.improvement import ImprovementRecord
from trading.domain.contracts.research import ExperimentProposal, ResearchHypothesis
from trading.domain.enums import (
    DeskRole,
    ExecutionMode,
    ImprovementArea,
    ImprovementStatus,
)
from trading.domain.enums import TestabilityKind as ClaimTestability
from trading.domain.primitives import Currency, Money

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


def _record(
    record_id: str,
    *,
    claim: str = "exits cut winners early",
    area: ImprovementArea = ImprovementArea.EXIT_RULE,
    trades: tuple[str, ...] = ("t1",),
    cost: str = "0.5",
    occurrences: int = 1,
    testable_as: ClaimTestability = ClaimTestability.SHADOW_RULE,
    status: ImprovementStatus = ImprovementStatus.OPEN,
) -> ImprovementRecord:
    return ImprovementRecord(
        record_id=record_id,
        opened_at=NOW - timedelta(days=7),
        author=DeskRole.POSTTRADE,
        area=area,
        claim=claim,
        supporting_trade_ids=trades,
        proposed_change="raise trail after +1R",
        testable_as=testable_as,
        status=status,
        occurrences=occurrences,
        estimated_cost_r=Decimal(cost),
        claim_key=normalize_claim_key(area, claim),
    )


def test_ineligible_stays_note() -> None:
    """Below min occurrences or NOT_TESTABLE → remains a note (no hypothesis)."""
    low = (
        _record("r1", claim="rare", occurrences=1),
        _record("r2", claim="rare", occurrences=1),
    )
    outcome = promote_improvements(low, as_of=NOW)
    assert outcome.hypotheses == ()
    assert outcome.experiments == ()
    assert len(outcome.note_clusters) >= 1

    untestable = (
        _record(
            "u1",
            claim="anecdote",
            occurrences=5,
            testable_as=ClaimTestability.NOT_TESTABLE,
        ),
    )
    outcome2 = promote_improvements(untestable, as_of=NOW)
    assert outcome2.hypotheses == ()
    assert outcome2.note_clusters


def test_eligible_promotes_to_hypothesis_and_shadow_experiment() -> None:
    records = (
        _record("r1", claim="A", cost="1.0", occurrences=2),
        _record("r2", claim="A", cost="1.0", occurrences=1),
    )
    outcome = promote_improvements(records, as_of=NOW)
    assert len(outcome.hypotheses) == 1
    hyp = outcome.hypotheses[0]
    assert hyp.status is ImprovementStatus.PROMOTED_TO_HYPOTHESIS
    assert hyp.occurrences >= 3
    assert len(outcome.experiments) == 1
    exp = outcome.experiments[0]
    assert exp.experiment.execution_mode is ExecutionMode.SHADOW
    assert exp.experiment.capital_limit.is_zero
    assert exp.status is ImprovementStatus.PROMOTED_TO_HYPOTHESIS
    # OPEN → CLUSTERED → PROMOTED on source records
    statuses = {r.record_id: r.status for r in outcome.clustered_records}
    assert statuses["r1"] is ImprovementStatus.PROMOTED_TO_HYPOTHESIS
    assert statuses["r2"] is ImprovementStatus.PROMOTED_TO_HYPOTHESIS


def test_shadow_capital_limit_zero_enforced() -> None:
    hyp = ResearchHypothesis(
        hypothesis_id="hyp:x",
        cluster_id="cluster:x",
        claim_key="EXIT_RULE:abc",
        area=ImprovementArea.EXIT_RULE,
        testable_as=ClaimTestability.SHADOW_RULE,
        status=ImprovementStatus.PROMOTED_TO_HYPOTHESIS,
        occurrences=3,
        estimated_cost_r_total=Decimal("1.5"),
        supporting_record_ids=("r1",),
    )
    # Builder always emits zero capital
    prop = hypothesis_to_shadow_experiment(hyp, started_at=NOW)
    assert prop.experiment.capital_limit == Money.zero(Currency.INR)

    # Existing ExperimentDefinition gate rejects SHADOW with capital > 0
    with pytest.raises(ValidationError, match="zero capital"):
        experiment(
            execution_mode=ExecutionMode.SHADOW,
            capital_limit=Money.of("1", Currency.INR),
        )

    # ExperimentProposal rejects non-SHADOW
    paper = experiment(execution_mode=ExecutionMode.PAPER)
    with pytest.raises(ValidationError, match="SHADOW"):
        ExperimentProposal(
            proposal_id="bad",
            hypothesis_id=hyp.hypothesis_id,
            experiment=paper,
            status=ImprovementStatus.PROMOTED_TO_HYPOTHESIS,
        )


def test_cluster_to_hypothesis_none_when_ineligible() -> None:
    from trading.analytics.improvements import cluster_improvements

    records = (_record("r1", occurrences=1),)
    cluster = cluster_improvements(records)[0]
    assert cluster_to_hypothesis(cluster, records) is None


def test_no_parallel_ladder_uses_existing_experiment_definition() -> None:
    records = (_record("r1", claim="B", occurrences=3),)
    outcome = promote_improvements(records, as_of=NOW)
    assert len(outcome.experiments) == 1
    assert isinstance(outcome.experiments[0].experiment, ExperimentDefinition)
    assert outcome.experiments[0].experiment.execution_mode is ExecutionMode.SHADOW
