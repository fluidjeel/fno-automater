"""ADESK-A7: ImprovementRecord dedupe, clustering, and store round-trip."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from trading.analytics.improvements import (
    apply_stale_status,
    cluster_improvements,
    hypothesis_eligible,
    merge_duplicate,
    normalize_claim_key,
)
from trading.domain.clock import FrozenClock
from trading.domain.contracts.improvement import ImprovementRecord
from trading.domain.enums import (
    DeskRole,
    ImprovementArea,
    ImprovementStatus,
    TestabilityKind as ClaimTestability,
)
from trading.storage.trading_store import TradingStore

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


def _record(
    record_id: str,
    *,
    claim: str = "exits cut winners early",
    area: ImprovementArea = ImprovementArea.EXIT_RULE,
    trades: tuple[str, ...] = ("t1",),
    cost: str = "0.5",
    occurrences: int = 1,
    opened_at: datetime = NOW,
) -> ImprovementRecord:
    return ImprovementRecord(
        record_id=record_id,
        opened_at=opened_at,
        author=DeskRole.POSTTRADE,
        area=area,
        claim=claim,
        supporting_trade_ids=trades,
        proposed_change="raise trail after +1R",
        testable_as=ClaimTestability.SHADOW_RULE,
        occurrences=occurrences,
        estimated_cost_r=Decimal(cost),
        claim_key=normalize_claim_key(area, claim),
    )


def test_requires_supporting_trade() -> None:
    with pytest.raises(ValidationError):
        ImprovementRecord(
            record_id="r0",
            opened_at=NOW,
            author=DeskRole.POSTTRADE,
            area=ImprovementArea.SIZING,
            claim="too big",
            supporting_trade_ids=(),
            proposed_change="cap size",
            testable_as=ClaimTestability.CONFIG_CHANGE,
            claim_key="x",
        )


def test_dedupe_merge_increments_occurrences() -> None:
    a = _record("r1", trades=("t1",))
    b = _record("r2", trades=("t2",), cost="1.0")
    merged = merge_duplicate(a, b)
    assert merged.occurrences == 2
    assert merged.supporting_trade_ids == ("t1", "t2")
    assert merged.estimated_cost_r == Decimal("1.0")


def test_cluster_ranks_by_occurrences_times_cost() -> None:
    key_a = normalize_claim_key(ImprovementArea.EXIT_RULE, "A")
    key_b = normalize_claim_key(ImprovementArea.SIZING, "B")
    records = (
        _record("r1", claim="A", cost="1.0", occurrences=2).model_copy(
            update={"claim_key": key_a}
        ),
        _record(
            "r2",
            claim="B",
            cost="5.0",
            occurrences=1,
            area=ImprovementArea.SIZING,
        ).model_copy(update={"claim_key": key_b}),
        _record("r3", claim="A", cost="1.0", occurrences=1).model_copy(
            update={"claim_key": key_a}
        ),
    )
    clusters = cluster_improvements(records)
    assert clusters[0].claim_key == key_a
    assert clusters[0].occurrences == 3
    assert clusters[0].rank_score == Decimal("3") * Decimal("2.0")


def test_hypothesis_and_stale_rules() -> None:
    open_once = _record("r1")
    assert hypothesis_eligible(open_once) is False
    recurrent = open_once.model_copy(update={"occurrences": 3})
    assert hypothesis_eligible(recurrent) is True
    old = _record("r2", opened_at=NOW - timedelta(days=100))
    stale = apply_stale_status((old,), as_of=NOW)
    assert stale[0].status is ImprovementStatus.STALE


def test_store_upsert_dedupes(tmp_path: Path) -> None:
    store = TradingStore.open(tmp_path / "t.sqlite", clock=FrozenClock(NOW))
    try:
        store.upsert_improvement_record(_record("r1", trades=("t1",)))
        second = store.upsert_improvement_record(
            _record("r2", trades=("t2",), cost="0.8")
        )
        assert second.occurrences == 2
        listed = store.list_improvement_records()
        assert len(listed) == 1
        assert listed[0].occurrences == 2
    finally:
        store.close()
