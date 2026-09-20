"""ADESK-D1: ConfidenceBucket enum + Phase-1 downscale-only sizing."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from tests import factories as f
from trading.ai.entry import build_shadow_entry_advice, sizing_advice_for_bucket
from trading.domain.contracts.advice import StructureChoice
from trading.domain.contracts.agent_decision import AgentDecision
from trading.domain.contracts.confidence_sizing import (
    PHASE1_M_CEILING,
    PHASE1_M_FLOOR,
    PHASE1_SIZE_MULTIPLIERS,
    Phase1SizingAdvice,
    bucket_from_confidence,
    phase1_size_multiplier,
)
from trading.domain.contracts.entry import (
    EntryAdvice,
    LegSpec,
    StrikeCandidate,
    StrikeShortlist,
)
from trading.domain.enums import (
    AgentAction,
    AuthorityMode,
    ConfidenceBucket,
    DeskRole,
    Environment,
    GateOutcome,
    LiquidityGrade,
    Side,
)
from trading.domain.primitives import Currency, Money, Percent

NOW = datetime(2026, 9, 20, 10, 0, tzinfo=UTC)
INR = Currency.INR


def _shortlist() -> StrikeShortlist:
    def cand(cid: str, score: str) -> StrikeCandidate:
        return StrikeCandidate(
            candidate_id=cid,
            legs=(
                LegSpec(
                    leg_id=cid,
                    contract=f.option_contract(),
                    side=Side.BUY,
                    ratio=1,
                ),
            ),
            net_debit=Money.of("5000", INR),
            max_loss=Money.of("5000", INR),
            bid_ask_spread_pct=Percent.from_percent("1"),
            breakeven_move_pct=Percent.from_percent("1.5"),
            deterministic_score=Decimal(score),
            liquidity_grade=LiquidityGrade.A,
        )

    return StrikeShortlist(
        snapshot_id="SNAP-D1",
        underlying="NIFTY",
        expiry=date(2026, 9, 24),
        structure=StructureChoice.POSITIONAL_LONG_OPTION,
        candidates=(cand("A", "0.9"), cand("B", "0.8")),
        shortlist_rule_version="shortlist-v1",
    )


class TestConfidenceBucket:
    def test_closed_set(self) -> None:
        assert {b.value for b in ConfidenceBucket} == {
            "0.1",
            "0.3",
            "0.5",
            "0.7",
            "0.9",
        }

    def test_unknown_string_rejected(self) -> None:
        with pytest.raises(ValueError, match="not a ConfidenceBucket"):
            bucket_from_confidence("0.55")

    def test_unknown_decimal_rejected(self) -> None:
        with pytest.raises(ValueError, match="fail closed"):
            bucket_from_confidence(Decimal("0.42"))


class TestPhase1Multipliers:
    def test_every_bucket_maps_and_is_at_most_one(self) -> None:
        for bucket in ConfidenceBucket:
            m = phase1_size_multiplier(bucket)
            assert PHASE1_M_FLOOR <= m <= PHASE1_M_CEILING
            assert m == PHASE1_SIZE_MULTIPLIERS[bucket]
            assert m <= Decimal("1")

    def test_table_covers_all_buckets(self) -> None:
        assert set(PHASE1_SIZE_MULTIPLIERS) == set(ConfidenceBucket)

    def test_advice_from_bucket_matches_table(self) -> None:
        for bucket in ConfidenceBucket:
            advice = Phase1SizingAdvice.from_bucket(bucket)
            assert advice.confidence_bucket is bucket
            assert advice.size_multiplier == PHASE1_SIZE_MULTIPLIERS[bucket]

    def test_mismatched_multiplier_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Phase1SizingAdvice(
                confidence_bucket=ConfidenceBucket.P50,
                size_multiplier=Decimal("1.00"),
            )

    def test_multiplier_above_one_rejected_by_type(self) -> None:
        with pytest.raises(ValidationError):
            Phase1SizingAdvice(
                confidence_bucket=ConfidenceBucket.P90,
                size_multiplier=Decimal("1.25"),
            )

    def test_entry_advice_rejects_multiplier_above_one(self) -> None:
        with pytest.raises(ValidationError):
            EntryAdvice(
                as_of=NOW,
                snapshot_id="SNAP-1",
                action=AgentAction.ABSTAIN,
                size_multiplier=Decimal("1.01"),
                narrative="too large",
            )

    def test_agent_decision_rejects_multiplier_above_one(self) -> None:
        with pytest.raises(ValidationError):
            AgentDecision(
                decision_id="DEC-1",
                run_id="RUN-1",
                role=DeskRole.ENTRY,
                mode=AuthorityMode.SHADOW,
                environment=Environment.PAPER,
                snapshot_id="SNAP-1",
                action=AgentAction.ABSTAIN,
                size_multiplier=Decimal("1.10"),
                agent_override=False,
                reason_codes=("TEST",),
                ungrounded_codes=(),
                evidence_ids=(),
                gate_outcome=GateOutcome.SHADOW_ONLY,
                model_id="m",
                prompt_version="p",
                policy_version="pol",
                packet_version="pkt",
                input_tokens=0,
                output_tokens=0,
                latency_ms=0,
                created_at=NOW,
            )


class TestEntryWiring:
    def test_shadow_entry_uses_phase1_bucket(self) -> None:
        advice = build_shadow_entry_advice(
            _shortlist(),
            as_of=NOW,
            confidence_bucket=ConfidenceBucket.P30,
        )
        assert advice is not None
        assert advice.size_multiplier == phase1_size_multiplier(ConfidenceBucket.P30)
        assert advice.size_multiplier <= Decimal("1")
        assert advice.thesis is not None
        assert advice.thesis.confidence == Decimal("0.3")

    def test_default_bucket_is_full_size_phase1(self) -> None:
        advice = build_shadow_entry_advice(_shortlist(), as_of=NOW)
        assert advice is not None
        assert advice.size_multiplier == Decimal("1.00")

    def test_sizing_advice_helper(self) -> None:
        shared = sizing_advice_for_bucket(ConfidenceBucket.P10)
        assert shared.size_multiplier == Decimal("0.50")
