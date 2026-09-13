"""Layer 2 contract boundaries and deterministic replay fixtures (L2-001).

Invariant 4: Layer 2 recalculates authoritative financial values in RiskDecision.
Invariant 14: capital reservation lifecycle is explicit in CapitalReservation.
Invariant 17: ExitPolicy stops never widen after entry.
Invariant 21: replay fixtures produce byte-identical RiskDecision serialization.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

import tests.factories as f
from trading.domain.contracts import (
    CapitalReservation,
    ContractError,
    ExitPolicy,
    OrderPlan,
    PortfolioSnapshot,
    PortfolioView,
    PositionState,
    ReconciliationResult,
    RiskDecision,
    SizingDecision,
    SizingRequest,
)
from trading.domain.enums import (
    ReasonCode,
    ReservationState,
    RiskAction,
    SystemState,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "l2_replay"

L2_VERSIONED_CONTRACTS = (
    PortfolioSnapshot,
    SizingRequest,
    SizingDecision,
    CapitalReservation,
    OrderPlan,
    PositionState,
    ExitPolicy,
    ReconciliationResult,
)

L2_CONTRACTS = (*L2_VERSIONED_CONTRACTS, PortfolioView)

L2_INSTANCES = (
    f.portfolio_snapshot(),
    f.portfolio_view(),
    f.sizing_request(),
    f.sizing_decision(),
    f.capital_reservation(),
    f.order_plan(),
    f.position_state(),
    f.exit_policy(),
    f.reconciliation_result(),
)


def _canonical_json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def _load_replay_fixture(name: str) -> RiskDecision:
    raw = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return RiskDecision.model_validate(raw["risk_decision"])


class TestLayer2ContractsRoundTrip:
    @pytest.mark.parametrize("instance", L2_INSTANCES, ids=lambda v: type(v).__name__)
    def test_json_round_trip_preserves_every_field(self, instance: Any) -> None:
        assert instance.round_trip() == instance

    @pytest.mark.parametrize(
        "model", L2_VERSIONED_CONTRACTS, ids=lambda m: m.__name__
    )
    def test_schema_version_is_declared(self, model: type[Any]) -> None:
        assert "schema_version" in model.model_fields


class TestLayer2ContractBoundaries:
    def test_portfolio_view_rejects_entries_when_system_not_ready(self) -> None:
        with pytest.raises(ValidationError, match="entries_permitted"):
            f.portfolio_view(system_state=SystemState.RECOVERY, entries_permitted=True)

    def test_sizing_request_requires_matching_snapshot_id(self) -> None:
        with pytest.raises(ValidationError, match="snapshot_id"):
            f.sizing_request(intent=f.intent(snapshot_id="OTHER"))

    def test_rejected_reservation_holds_no_capital(self) -> None:
        with pytest.raises(ValidationError, match="must not hold capital"):
            f.capital_reservation(
                state=ReservationState.REJECTED,
                amount=f.money("1000"),
                reason_codes=(ReasonCode.RISK_LIMIT_TRADE,),
            )

    def test_exit_policy_rejects_widening_stop(self) -> None:
        with pytest.raises(ValidationError, match="may only tighten"):
            f.exit_policy(current_stop_distance_ticks=250)

    def test_open_position_requires_protective_order_refs(self) -> None:
        with pytest.raises(ValidationError, match="protective coverage"):
            f.position_state(protective_order_ids=())

    def test_reconciliation_result_blocks_ready_with_entries_blocked(self) -> None:
        with pytest.raises(ValidationError, match="READY cannot coexist"):
            f.reconciliation_result(
                resulting_system_state=SystemState.READY,
                entries_blocked=True,
            )


class TestLayer2ReplayFixtures:
    def test_approved_long_call_fixture_is_byte_stable(self) -> None:
        first = _load_replay_fixture("approved_long_call")
        second = _load_replay_fixture("approved_long_call")
        first_bytes = _canonical_json_bytes(first.model_dump(mode="json"))
        second_bytes = _canonical_json_bytes(second.model_dump(mode="json"))
        assert first_bytes == second_bytes
        assert first.action is RiskAction.APPROVE
        assert first.capital_reservation_id is not None

    def test_rejected_margin_fixture_is_byte_stable(self) -> None:
        first = _load_replay_fixture("rejected_margin")
        second = _load_replay_fixture("rejected_margin")
        first_bytes = _canonical_json_bytes(first.model_dump(mode="json"))
        second_bytes = _canonical_json_bytes(second.model_dump(mode="json"))
        assert first_bytes == second_bytes
        assert first.action is RiskAction.REJECT
        assert ReasonCode.MARGIN_INSUFFICIENT in first.reason_codes

    def test_replay_fixtures_reject_float_injection(self) -> None:
        raw = json.loads(
            (FIXTURES / "approved_long_call.json").read_text(encoding="utf-8")
        )
        raw["risk_decision"]["reserved_capital"]["amount"] = 10000.0
        with pytest.raises((ValidationError, ContractError)):
            RiskDecision.model_validate(raw["risk_decision"])
