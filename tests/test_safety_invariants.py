"""One named test per testable safety invariant.

docs/context/SAFETY_INVARIANTS.md numbers 25 properties and states that "any code
review touching these invariants requires explicit failure tests". This module is
the index: each test names the invariant it covers, and a meta-test parses the
specification so an invariant cannot be added without being classified as either
covered here or explicitly deferred to a later phase.

Invariants that need a running feed, broker, storage layer or promotion pipeline
cannot be tested at Phase 0. They are listed in DEFERRED with the phase that owns
them, rather than silently omitted.
"""

from __future__ import annotations

import re
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

import tests.factories as f
from trading.broker.paper import PaperBroker
from trading.config import (
    ConfigNotVerifiedError,
    Environment,
    load_config,
    load_config_text,
)
from trading.domain.clock import FrozenClock
from trading.domain.contracts import (
    AIProposal,
    DataQualityReport,
    ExitTemplate,
    FeatureSnapshot,
    Lineage,
    OrderEvent,
    ReconciliationEvent,
    RiskDecision,
    SnapshotTimes,
    TradeIntent,
)
from trading.domain.enums import (
    DataQuality,
    OrderState,
    ReasonCode,
    Recommendation,
    RiskAction,
    SystemState,
    TradeState,
    Trigger,
)
from trading.domain.ids import SequentialIdFactory, derive_idempotency_key
from trading.domain.primitives import Money, Rounding
from trading.domain.state import (
    ORDER_MACHINE,
    SYSTEM_MACHINE,
    TRADE_MACHINE,
    IllegalTransitionError,
)
from trading.portfolio import build_broker_snapshot
from trading.safety import SafetyControls

SPEC = (
    Path(__file__).resolve().parent.parent / "docs" / "context" / "SAFETY_INVARIANTS.md"
)
BASE_CONFIG = Path(__file__).resolve().parent.parent / "config" / "base.yaml"
BROKER_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "broker"

# Invariants this module tests at Phase 0.
COVERED = frozenset(
    {2, 3, 4, 5, 6, 7, 9, 11, 12, 13, 14, 16, 17, 18, 19, 21, 23, 24, 25}
)

# Invariants that require a component Phase 0 does not build yet.
DEFERRED: dict[int, str] = {
    1: "Phase 3: needs an OMS and a live decision path to constrain",
    8: "Phase 3: needs a running trade manager to keep protecting positions",
    10: "Phase 2: needs a durable store and a real clock-drift monitor",
    15: "Phase 3: needs an execution planner to invoke the repair policy",
    20: "Phase 1: needs the replay harness to check for lookahead",
    22: "Phase 6: needs the evaluator and the promotion pipeline",
}


def _specified_invariants() -> frozenset[int]:
    text = SPEC.read_text(encoding="utf-8")
    return frozenset(int(n) for n in re.findall(r"^(\d+)\.\s+\S", text, re.MULTILINE))


class TestInvariantCoverageIsAuditable:
    def test_the_specification_still_lists_twenty_five_invariants(self) -> None:
        assert len(_specified_invariants()) == 25

    def test_every_specified_invariant_is_covered_or_explicitly_deferred(self) -> None:
        specified = _specified_invariants()
        classified = COVERED | frozenset(DEFERRED)
        assert specified - classified == frozenset(), (
            "these invariants are neither tested nor explicitly deferred: "
            f"{sorted(specified - classified)}"
        )
        assert classified - specified == frozenset(), (
            f"these are classified but no longer specified: "
            f"{sorted(classified - specified)}"
        )

    def test_no_invariant_is_both_covered_and_deferred(self) -> None:
        assert not COVERED & frozenset(DEFERRED)

    def test_each_covered_invariant_has_a_named_test(self) -> None:
        source = Path(__file__).read_text(encoding="utf-8")
        for number in sorted(COVERED):
            assert f"def test_invariant_{number:02d}_" in source, (
                f"invariant {number} is marked covered but has no "
                f"test_invariant_{number:02d}_* test in this module"
            )


class TestAuthority:
    def test_invariant_02_ai_cannot_reach_the_broker_or_mutate_state(self) -> None:
        """AI cannot call broker tools, mutate live state or bypass a gate."""
        forbidden = {
            "quantity",
            "lots",
            "contracts",
            "order",
            "stop",
            "target",
            "broker",
            "promoted",
            "approved",
            "reservation",
        }
        for name in AIProposal.model_fields:
            assert not forbidden & set(name.lower().split("_")), (
                f"AIProposal.{name} would give AI a live lever"
            )

    def test_invariant_03_strategies_cannot_submit_orders(self) -> None:
        """Strategies emit Trade Intents only."""
        names = {n.replace("_", "") for n in TradeIntent.model_fields}
        assert (
            not {
                "quantity",
                "lots",
                "contracts",
                "ordertype",
                "brokerorderid",
                "clientorderid",
                "brokertoken",
                "approvedquantity",
            }
            & names
        )

    def test_invariant_04_layer_two_recalculates_authoritative_values(self) -> None:
        """An approval is invalid without Layer 2's own max loss and margin."""
        with pytest.raises(ValidationError, match="own max-loss"):
            f.risk_decision(recalculated_max_loss=None)
        with pytest.raises(ValidationError, match="own max-loss"):
            f.risk_decision(margin_required=None)

    def test_invariant_05_broker_truth_is_external(self) -> None:
        """Broker-reported funds and positions are the portfolio source of truth."""
        clock = FrozenClock(f.NOW)
        ids = SequentialIdFactory(clock.instant)
        broker = PaperBroker.from_fixtures(BROKER_FIXTURES, clock=clock, id_factory=ids)
        snapshot = build_broker_snapshot(
            broker,
            account_id="ACC-PAPER-1",
            versions=f.versions(),
            id_factory=ids,
            reserved_capital=f.money("0"),
        )
        assert snapshot.exposure.equity == broker.get_funds().equity
        assert snapshot.positions == broker.get_positions()


class TestFailClosed:
    def test_invariant_06_invalid_critical_state_blocks_new_exposure(self) -> None:
        """Unknown, stale, inconsistent or invalid state blocks new exposure."""
        for state in (DataQuality.STALE, DataQuality.INVALID):
            report = DataQualityReport(
                state=state,
                age_ms=10_000,
                warmup_complete=True,
                source_status="degraded",
                reason_codes=(ReasonCode.DATA_STALE,),
            )
            assert not report.permits_new_exposure
        assert f.snapshot().permits_new_exposure

    def test_invariant_07_missing_or_late_ai_output_is_not_fatal(self) -> None:
        """Missing, invalid or late AI output falls back rather than failing."""
        abstained = f.proposal(recommendation=Recommendation.ABSTAIN, evidence=())
        assert not abstained.is_usable_at(f.NOW)

        expired = f.proposal(valid_until=f.NOW + timedelta(minutes=1))
        assert not expired.is_usable_at(f.NOW + timedelta(hours=1))

        # An intent needs no proposal at all, so absence cannot block protection.
        assert f.intent().promoted_proposal_id is None

    def test_invariant_09_startup_begins_in_recovery(self) -> None:
        """Startup, restart and reconnect begin in RECOVERY; entries wait."""
        assert not SYSTEM_MACHINE.can(
            SystemState.STARTING, SystemState.READY, Trigger.STARTUP
        )
        assert SYSTEM_MACHINE.can(
            SystemState.STARTING, SystemState.RECOVERY, Trigger.STARTUP
        )
        assert SYSTEM_MACHINE.allowed_triggers(
            SystemState.RECOVERY, SystemState.READY
        ) == frozenset({Trigger.RECONCILIATION})
        assert not SystemState.RECOVERY.permits_new_exposure


class TestOrdersAndCapital:
    def test_invariant_11_one_logical_order_keeps_one_idempotency_key(self) -> None:
        """One stable idempotency key across retries."""
        order = {
            "account_id": "ACC-1",
            "strategy_id": "s",
            "strategy_version": "1",
            "intent_id": "INT-1",
            "leg_id": "leg-1",
            "side": "BUY",
            "quantity_contracts": 75,
        }
        keys = {derive_idempotency_key(**order) for _ in range(10)}  # type: ignore[arg-type]
        assert len(keys) == 1

        first = f.order_event(attempt_number=1)
        retry = f.order_event(attempt_number=2)
        assert first.identity.idempotency_key == retry.identity.idempotency_key

    def test_invariant_12_a_timeout_never_proves_a_fill(self) -> None:
        """Timeout or acknowledgement never proves a fill."""
        for source in (
            OrderState.SUBMITTING,
            OrderState.ACKNOWLEDGED,
            OrderState.PARTIAL,
            OrderState.CANCEL_PENDING,
        ):
            assert not ORDER_MACHINE.can(source, OrderState.FILLED, Trigger.TIMEOUT)
        with pytest.raises(ValidationError, match="invariant 12"):
            f.order_event(
                state=OrderState.UNKNOWN,
                filled_quantity=75,
                average_fill_price=f.price("120.00"),
                reason_code=ReasonCode.ORDER_TIMEOUT,
            )

    def test_invariant_13_unknown_blocks_replacement_until_reconciliation(
        self,
    ) -> None:
        """An unknown submit outcome cannot be replaced by a blind retry."""
        assert OrderState.SUBMITTING not in ORDER_MACHINE.allowed_targets(
            OrderState.UNKNOWN
        )
        for target in ORDER_MACHINE.allowed_targets(OrderState.UNKNOWN):
            assert ORDER_MACHINE.allowed_triggers(
                OrderState.UNKNOWN, target
            ) == frozenset({Trigger.RECONCILIATION})
        with pytest.raises(IllegalTransitionError):
            ORDER_MACHINE.transition(
                OrderState.UNKNOWN,
                OrderState.FILLED,
                trigger=Trigger.LOCAL_COMMAND,
                at=f.NOW,
            )

    def test_invariant_14_capital_is_reserved_before_submission(self) -> None:
        """Reserved before submission, released or adjusted on confirmed events."""
        with pytest.raises(ValidationError, match="requires a capital reservation"):
            f.risk_decision(capital_reservation_id=None, reserved_capital=None)

        refusal = f.risk_decision(
            action=RiskAction.REJECT,
            approved_legs=(),
            capital_reservation_id=None,
            reserved_capital=None,
            recalculated_max_loss=None,
            margin_required=None,
            post_trade_projection=None,
            reason_codes=(ReasonCode.RISK_LIMIT_PORTFOLIO,),
        )
        assert refusal.reserved_capital is None
        assert not refusal.permits_submission

    def test_invariant_14_no_duplicate_or_negative_reservation(self) -> None:
        """A negative or zero reservation is rejected, as is a duplicate leg."""
        with pytest.raises(ValidationError, match="must be positive"):
            f.risk_decision(reserved_capital=f.money("-1"))
        with pytest.raises(ValidationError, match="unique"):
            f.risk_decision(
                approved_legs=(f.approved_leg("leg-1"), f.approved_leg("leg-1"))
            )

    def test_invariant_16_every_open_position_has_protective_coverage(self) -> None:
        """Every open position maps to active deterministic protection."""
        # A stop is mandatory on the exit template, so no intent can reach Layer 2
        # without one.
        assert ExitTemplate.model_fields["stop_distance_ticks"].is_required()
        with pytest.raises(ValidationError):
            f.exit_template(stop_distance_ticks=0)

        # And no state that needs protection is terminal, so protection always has
        # somewhere to act.
        for state in TradeState:
            if state.requires_protective_coverage:
                assert state not in TRADE_MACHINE.terminal

    def test_invariant_16_max_loss_must_be_defined(self) -> None:
        """An undefined worst case cannot be sized or protected."""
        with pytest.raises(ValidationError, match="must be positive and defined"):
            f.intent(estimated_max_loss=f.money("0"))

    def test_invariant_17_stops_never_widen(self) -> None:
        """Stops never widen after entry unless a versioned policy authorizes it."""
        with pytest.raises(ValidationError, match="may only tighten"):
            f.exit_template(
                stop_distance_ticks=100,
                trailing_activation_ticks=50,
                trailing_distance_ticks=101,
            )
        names = {n.replace("_", "") for n in ExitTemplate.model_fields}
        assert not {"widen", "loosen", "relaxstop", "stopwidening"} & names


class TestDataAndReplay:
    def test_invariant_18_one_snapshot_with_unhidden_timestamps(self) -> None:
        """A decision references one immutable snapshot; times are never merged."""
        assert f.intent().snapshot_id == "SNAP-1"
        times = FeatureSnapshot.model_fields["times"]
        assert times.is_required()
        for field in ("event_time", "source_time", "receive_time", "calculation_time"):
            assert field in SnapshotTimes.model_fields
        with pytest.raises(ValidationError, match="precedes event_time"):
            f.snapshot(times=f.snapshot_times(receive_time=f.NOW - timedelta(hours=1)))

    def test_invariant_19_critical_values_carry_validity_and_lineage(self) -> None:
        """Freshness, validity, unit, time and lineage travel with the value."""
        snapshot = f.snapshot()
        assert snapshot.quality.state is DataQuality.VALID
        assert snapshot.quality.age_ms >= 0
        assert snapshot.lineage.versions.config_checksum
        assert snapshot.times.event_time.tzinfo is not None
        # Units are types, not conventions.
        assert snapshot.market.bid is not None
        assert snapshot.market.bid.tick.value > 0
        for name in ("provider", "normalization_version", "versions"):
            assert Lineage.model_fields[name].is_required()

    def test_invariant_21_same_inputs_reproduce_the_same_output(self) -> None:
        """Same snapshot, config and code version yields the same decision."""
        # Identifiers are deterministic given the same factory state.
        assert SequentialIdFactory(f.NOW).new_id("x") == SequentialIdFactory(
            f.NOW
        ).new_id("x")
        # Serialization is stable, so a replayed payload is byte-comparable.
        assert f.intent().model_dump_json() == f.intent().model_dump_json()
        assert f.intent().round_trip() == f.intent()
        # Config identity is stable across loads.
        assert load_config(BASE_CONFIG).checksum == load_config(BASE_CONFIG).checksum

    def test_invariant_21_rounding_is_deterministic_and_does_not_bias(self) -> None:
        """Half-even everywhere, so ties do not drift sizing upward over time."""
        assert Money.of("0.125", f.INR).quantized().amount == Decimal("0.12")
        assert Money.of("0.135", f.INR).quantized().amount == Decimal("0.14")
        assert Money.of("-1.111", f.INR).quantized(
            Rounding.TOWARD_ZERO
        ).amount == Decimal("-1.11")


class TestChangeControl:
    def test_invariant_23_config_changes_carry_a_version_and_checksum(self) -> None:
        """Live config changes require validation, version and checksum."""
        loaded = load_config(BASE_CONFIG)
        assert loaded.version and len(loaded.checksum) == 64
        assert loaded.lineage == (loaded.version, loaded.checksum)
        edited = load_config_text(
            BASE_CONFIG.read_text(encoding="utf-8") + "\n# edit\n"
        )
        assert edited.checksum != loaded.checksum

    def test_invariant_23_unverified_config_cannot_go_live(self) -> None:
        """A live account will not start on an unverified market rule."""
        config = load_config(BASE_CONFIG).config
        with pytest.raises(ConfigNotVerifiedError):
            config.require_ready_for(Environment.LIVE)

    def test_invariant_24_kill_switch_actions_are_independently_callable(
        self,
    ) -> None:
        """Kill-switch actions are deterministic, independently callable and tested."""
        clock = FrozenClock(f.NOW)
        controls = SafetyControls(
            clock=clock,
            id_factory=SequentialIdFactory(clock.instant),
        )
        event = controls.freeze_entries(
            actor="operator",
            scope="account/ACC-1",
        )
        assert controls.blocks_entry()
        assert ReasonCode.ENTRY_FROZEN in event.reason_codes
        controls.release_entry_freeze(
            actor="operator",
            scope="account/ACC-1",
        )
        assert not controls.blocks_entry()

    def test_invariant_25_every_transition_is_auditable(self) -> None:
        """Every decision, order and recovery transition is durably auditable."""
        record = ORDER_MACHINE.transition(
            OrderState.CREATED,
            OrderState.SUBMITTING,
            trigger=Trigger.LOCAL_COMMAND,
            at=f.NOW,
        )
        assert record.allowed and record.reason_code is ReasonCode.OK

        # A rejected transition produces the same evidence rather than vanishing.
        with pytest.raises(IllegalTransitionError) as caught:
            ORDER_MACHINE.transition(
                OrderState.CREATED,
                OrderState.FILLED,
                trigger=Trigger.BROKER_EVENT,
                at=f.NOW,
            )
        rejected = caught.value.record
        assert rejected.allowed is False
        assert rejected.reason_code is ReasonCode.ILLEGAL_STATE_TRANSITION
        assert rejected.at.tzinfo is not None
        assert rejected.detail

        # Every contract that records an outcome demands a machine-readable reason.
        assert RiskDecision.model_fields["reason_codes"].is_required()
        assert ReconciliationEvent.model_fields["reason_code"].is_required()
        assert "received_at" in OrderEvent.model_fields
