"""Contract boundaries: closed schemas, lossless round-trip, fail-closed rules.

Invariant 3: strategies emit intents only; they cannot express a broker command.
Invariant 4: Layer 2 recalculates every authoritative financial value.
Invariant 6: unknown, stale or invalid critical state blocks new exposure.
Invariant 7: missing or late AI output falls back; it is never fatal.
Invariant 12: a timeout or acknowledgement never proves a fill.
Invariant 14: capital is reserved before submission and released on refusal.
Invariant 17: stops never widen after entry.
Invariant 18: one decision references one immutable versioned snapshot.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import BaseModel, ValidationError

import tests.factories as f
from trading.domain.contracts import (
    SCHEMA_VERSION,
    AIProposal,
    ContractError,
    DerivativesContext,
    ExitTemplate,
    FamilyAction,
    FeatureSnapshot,
    Greeks,
    IntentLeg,
    OrderEvent,
    OrderIdentity,
    ParameterProposal,
    ReconciliationEvent,
    RiskDecision,
    TradeIntent,
)
from trading.domain.enums import (
    DataQuality,
    DifferenceClass,
    FamilyStance,
    OrderState,
    OrderType,
    ProposalType,
    ReasonCode,
    Recommendation,
    RiskAction,
    Severity,
    Side,
)
from trading.domain.primitives import Currency, Money, Percent

CONTRACTS = (
    FeatureSnapshot,
    AIProposal,
    TradeIntent,
    RiskDecision,
    OrderEvent,
    ReconciliationEvent,
)

INSTANCES = (
    f.snapshot(),
    f.proposal(),
    f.intent(),
    f.risk_decision(),
    f.order_event(),
    f.reconciliation_event(),
)


def _all_models(model: type[BaseModel], seen: set[type[BaseModel]]) -> None:
    if model in seen:
        return
    seen.add(model)
    for info in model.model_fields.values():
        for candidate in (info.annotation, *getattr(info.annotation, "__args__", ())):
            if isinstance(candidate, type) and issubclass(candidate, BaseModel):
                _all_models(candidate, seen)


ALL_NESTED: set[type[BaseModel]] = set()
for contract in CONTRACTS:
    _all_models(contract, ALL_NESTED)


def type_name(value: Any) -> str:
    """Readable parametrize id for a model class or instance."""
    return getattr(value, "__name__", type(value).__name__)


NESTED_MODELS = sorted(ALL_NESTED, key=lambda model: model.__name__)


class TestSchemaHygiene:
    @pytest.mark.parametrize("model", NESTED_MODELS, ids=type_name)
    def test_every_model_forbids_unknown_fields(self, model: type[BaseModel]) -> None:
        assert model.model_config.get("extra") == "forbid"

    @pytest.mark.parametrize("model", NESTED_MODELS, ids=type_name)
    def test_every_model_is_frozen(self, model: type[BaseModel]) -> None:
        assert model.model_config.get("frozen") is True

    @pytest.mark.parametrize("model", NESTED_MODELS, ids=type_name)
    def test_no_field_is_a_float(self, model: type[BaseModel]) -> None:
        """A float field would reintroduce binary rounding into accounting."""
        for name, info in model.model_fields.items():
            annotation = info.annotation
            candidates = (annotation, *getattr(annotation, "__args__", ()))
            assert float not in candidates, f"{model.__name__}.{name} is a float"

    @pytest.mark.parametrize("model", CONTRACTS, ids=type_name)
    def test_top_level_contracts_declare_a_schema_version(
        self, model: type[BaseModel]
    ) -> None:
        assert "schema_version" in model.model_fields

    @pytest.mark.parametrize("instance", INSTANCES, ids=type_name)
    def test_unknown_field_is_rejected(self, instance: BaseModel) -> None:
        payload = instance.model_dump(mode="json")
        payload["surprise"] = "value"
        with pytest.raises(ValidationError, match="surprise"):
            type(instance).model_validate(payload)

    @pytest.mark.parametrize("instance", INSTANCES, ids=type_name)
    def test_mutation_after_construction_is_rejected(self, instance: BaseModel) -> None:
        with pytest.raises(ValidationError):
            instance.schema_version = "99"  # type: ignore[attr-defined]

    @pytest.mark.parametrize("instance", INSTANCES, ids=type_name)
    def test_schema_version_defaults_to_the_current_one(
        self, instance: BaseModel
    ) -> None:
        assert instance.schema_version == SCHEMA_VERSION  # type: ignore[attr-defined]


class TestLosslessRoundTrip:
    @pytest.mark.parametrize("instance", INSTANCES, ids=type_name)
    def test_json_round_trip_preserves_every_field(self, instance: Any) -> None:
        assert instance.round_trip() == instance

    @pytest.mark.parametrize("instance", INSTANCES, ids=type_name)
    def test_round_trip_is_stable_under_repetition(self, instance: Any) -> None:
        assert instance.round_trip().round_trip() == instance

    def test_decimal_precision_survives_serialization(self) -> None:
        """Money that serializes to a float would lose paise."""
        precise = f.intent(requested_risk=Money.of("7000.005", f.INR))
        assert precise.round_trip().requested_risk.amount == Decimal("7000.005")

    def test_tick_grid_survives_serialization(self) -> None:
        assert f.order_event().round_trip().command.limit_price == f.price("120.00")


class TestFloatIsRejectedEverywhere:
    """No float ever reaches an accounting path, including nested in a dict."""

    def test_float_in_a_nested_money_is_rejected(self) -> None:
        payload = f.intent().model_dump(mode="json")
        payload["requested_risk"]["amount"] = 7000.5
        with pytest.raises(ValidationError, match="float is not permitted"):
            TradeIntent.model_validate(payload)

    def test_float_in_a_feature_map_is_rejected(self) -> None:
        payload = f.snapshot().model_dump(mode="json")
        payload["features"]["realized_vol_20d"] = 0.14
        with pytest.raises(ValidationError, match="float is not permitted"):
            FeatureSnapshot.model_validate(payload)

    def test_float_in_a_list_is_rejected(self) -> None:
        payload = f.intent().model_dump(mode="json")
        payload["legs"][0]["ratio"] = 1.0
        with pytest.raises(ValidationError, match="float is not permitted"):
            TradeIntent.model_validate(payload)

    def test_float_confidence_is_rejected(self) -> None:
        payload = f.proposal().model_dump(mode="json")
        payload["confidence"] = 0.6
        with pytest.raises(ValidationError, match="float is not permitted"):
            AIProposal.model_validate(payload)

    def test_the_error_names_the_path(self) -> None:
        payload = f.intent().model_dump(mode="json")
        payload["requested_risk"]["amount"] = 1.5
        with pytest.raises(ValidationError, match=r"requested_risk\.amount"):
            TradeIntent.model_validate(payload)


class TestNaiveTimeAndNonFiniteValues:
    @pytest.mark.parametrize("instance", INSTANCES, ids=type_name)
    def test_naive_timestamps_are_rejected(self, instance: BaseModel) -> None:
        payload = instance.model_dump(mode="json")
        time_fields = [
            name
            for name in payload
            if isinstance(payload[name], str) and payload[name].endswith("Z")
        ]
        for name in time_fields:
            broken = dict(payload)
            broken[name] = payload[name].rstrip("Z")
            with pytest.raises(ValidationError, match="naive"):
                type(instance).model_validate(broken)

    def test_nan_decimal_is_rejected(self) -> None:
        payload = f.snapshot().model_dump(mode="json")
        payload["features"]["realized_vol_20d"] = "NaN"
        with pytest.raises(ValidationError, match="finite"):
            FeatureSnapshot.model_validate(payload)

    def test_infinity_decimal_is_rejected(self) -> None:
        payload = f.snapshot().model_dump(mode="json")
        payload["features"]["realized_vol_20d"] = "Infinity"
        with pytest.raises(ValidationError, match="finite"):
            FeatureSnapshot.model_validate(payload)


class TestFuzzMalformedPayloads:
    """A malformed payload yields a validation error, never a partial object."""

    @pytest.mark.parametrize("instance", INSTANCES, ids=type_name)
    def test_dropping_any_required_field_raises(self, instance: BaseModel) -> None:
        model = type(instance)
        required = [
            name for name, info in model.model_fields.items() if info.is_required()
        ]
        assert required, f"{model.__name__} has no required fields"
        for name in required:
            payload = instance.model_dump(mode="json")
            del payload[name]
            with pytest.raises(ValidationError):
                model.model_validate(payload)

    @pytest.mark.parametrize("instance", INSTANCES, ids=type_name)
    def test_replacing_any_field_with_a_wrong_type_raises(
        self, instance: BaseModel
    ) -> None:
        model = type(instance)
        # A mapping to a list is invalid for every shape used in these contracts:
        # scalars, tuples of scalars, dicts of Decimal, and nested models (which
        # forbid extra keys). A bare list would be accepted by tuple fields.
        sentinel = {"__unexpected__": ["shape"]}
        for name in model.model_fields:
            payload = instance.model_dump(mode="json")
            payload[name] = sentinel
            with pytest.raises(ValidationError):
                model.model_validate(payload)

    @pytest.mark.parametrize("model", CONTRACTS, ids=type_name)
    @given(st.dictionaries(st.text(max_size=8), st.integers(), max_size=4))
    def test_arbitrary_payloads_never_construct_an_object(
        self, model: type[BaseModel], payload: dict[str, int]
    ) -> None:
        with pytest.raises((ValidationError, ContractError)):
            model.model_validate(payload)

    @pytest.mark.parametrize("instance", INSTANCES, ids=type_name)
    def test_empty_identifier_is_rejected(self, instance: BaseModel) -> None:
        payload = instance.model_dump(mode="json")
        id_fields = [n for n in payload if n.endswith("_id") and payload[n]]
        for name in id_fields:
            broken = dict(payload)
            broken[name] = "   "
            with pytest.raises(ValidationError, match="empty"):
                type(instance).model_validate(broken)


class TestFeatureSnapshot:
    def test_causality_between_timestamps_is_enforced(self) -> None:
        """Invariant 18: mixed timestamps are never hidden."""
        with pytest.raises(ValidationError, match="precedes event_time"):
            f.snapshot(times=f.snapshot_times(receive_time=f.NOW - timedelta(hours=1)))

    def test_crossed_book_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="crossed book"):
            f.snapshot(market=f.quote(bid=f.price("100.10"), ask=f.price("100.00")))

    def test_stale_quality_blocks_new_exposure(self) -> None:
        """Invariant 6."""
        stale = f.snapshot(
            quality=f.quality(
                state=DataQuality.STALE, reason_codes=(ReasonCode.DATA_STALE,)
            )
        )
        assert not stale.permits_new_exposure

    def test_valid_quality_permits_exposure(self) -> None:
        assert f.snapshot().permits_new_exposure

    def test_non_valid_quality_requires_a_reason_code(self) -> None:
        with pytest.raises(ValidationError, match="requires at least one reason code"):
            f.quality(state=DataQuality.DEGRADED)

    def test_valid_state_cannot_have_incomplete_warmup(self) -> None:
        with pytest.raises(ValidationError, match="incomplete warm-up"):
            f.quality(warmup_complete=False)

    def test_option_snapshot_requires_derivatives_context(self) -> None:
        with pytest.raises(ValidationError, match="requires a DerivativesContext"):
            f.snapshot(contract=f.option_contract())

    def test_index_snapshot_must_not_carry_derivatives_context(self) -> None:

        with pytest.raises(ValidationError, match="must not carry"):
            f.snapshot(derivatives=DerivativesContext(days_to_expiry=10))

    def test_non_converged_greeks_publish_nothing(self) -> None:

        with pytest.raises(ValidationError, match="must not publish Greeks"):
            Greeks(
                model="bsm",
                calculation_version="1",
                converged=False,
                delta=Decimal("0.5"),
            )

    def test_option_contract_requires_strike_and_type(self) -> None:
        with pytest.raises(ValidationError, match="requires both strike"):
            f.option_contract(strike=None)

    def test_index_contract_rejects_a_strike(self) -> None:
        with pytest.raises(ValidationError, match="must not carry strike"):
            f.index_contract(strike=Decimal("24000"))

    def test_snapshot_age_uses_an_injected_instant(self) -> None:
        assert f.snapshot().times.age_at(f.NOW + timedelta(seconds=5)) == timedelta(
            seconds=5
        )


class TestAIProposal:
    def test_a_proposal_cannot_promote_itself(self) -> None:
        """Invariant 22: AI output is a proposal, never a deployment."""
        forbidden = {"promoted", "approved", "deploy", "activate", "live"}
        for name in AIProposal.model_fields:
            assert not (forbidden & set(name.lower().split("_")))

    def test_a_proposal_cannot_name_a_quantity_or_an_order(self) -> None:
        forbidden = {
            "quantity",
            "qty",
            "contracts",
            "lots",
            "order",
            "stop",
            "target",
            "broker",
        }
        for name in AIProposal.model_fields:
            assert not (forbidden & set(name.lower().split("_")))

    def test_expiry_is_mandatory_and_positive(self) -> None:
        with pytest.raises(ValidationError, match="valid_until must be after"):
            f.proposal(valid_until=f.NOW)

    def test_expired_proposal_is_not_usable(self) -> None:
        """Invariant 7: a late proposal falls back rather than being consumed."""
        stale = f.proposal(valid_until=f.NOW + timedelta(minutes=1))
        assert not stale.is_usable_at(f.NOW + timedelta(hours=1))
        assert stale.is_usable_at(f.NOW)

    def test_abstain_needs_no_evidence_and_is_not_an_error(self) -> None:
        abstained = f.proposal(recommendation=Recommendation.ABSTAIN, evidence=())
        assert not abstained.is_usable_at(f.NOW)

    def test_a_recommendation_without_evidence_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="requires at least one"):
            f.proposal(evidence=())

    def test_evidence_published_after_as_of_time_is_rejected(self) -> None:
        """Time-bounded retrieval: lookahead in research, manipulation when live."""
        future = f.evidence(
            published_at=f.NOW + timedelta(days=1),
            retrieved_at=f.NOW + timedelta(days=1),
        )
        with pytest.raises(ValidationError, match="later than as_of_time"):
            f.proposal(evidence=(future,))

    def test_unlisted_source_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="not allowlisted"):
            f.evidence(allowlisted=False)

    def test_confidence_outside_zero_to_one_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            f.proposal(confidence=Decimal("1.5"))

    def test_abstaining_proposal_cannot_propose_parameters(self) -> None:

        with pytest.raises(ValidationError, match="must not propose parameters"):
            f.proposal(
                recommendation=Recommendation.ABSTAIN,
                evidence=(),
                parameter_proposals=(
                    ParameterProposal(
                        field_path="a.b",
                        proposed_value=Decimal("1"),
                        allowed_minimum=Decimal("0"),
                        allowed_maximum=Decimal("2"),
                    ),
                ),
            )

    def test_strategy_family_requires_unique_actions(self) -> None:
        action = FamilyAction(
            strategy_id="positional_long_option", stance=FamilyStance.ENABLE
        )
        with pytest.raises(ValidationError, match="at least one family action"):
            f.proposal(
                proposal_type=ProposalType.STRATEGY_FAMILY,
                recommendation=Recommendation.REVISE,
                family_actions=(),
            )
        with pytest.raises(ValidationError, match="must be unique"):
            f.proposal(
                proposal_type=ProposalType.STRATEGY_FAMILY,
                recommendation=Recommendation.REVISE,
                family_actions=(action, action),
            )

    def test_parameter_outside_its_allowed_range_is_rejected(self) -> None:

        with pytest.raises(ValidationError, match="outside the allowed range"):
            ParameterProposal(
                field_path="kelly.fraction",
                proposed_value=Decimal("5"),
                allowed_minimum=Decimal("0"),
                allowed_maximum=Decimal("1"),
            )


class TestTradeIntent:
    def test_intent_cannot_express_a_broker_command(self) -> None:
        """Invariant 3. The absent fields are the mechanism, so assert absence."""
        forbidden = {
            "quantity",
            "qty",
            "contracts",
            "lots",
            "brokerorderid",
            "clientorderid",
            "ordertype",
            "approvedquantity",
            "brokertoken",
        }
        names = {n.replace("_", "") for n in TradeIntent.model_fields}
        assert not (forbidden & names)

    def test_legs_carry_a_ratio_not_a_quantity(self) -> None:

        assert "ratio" in IntentLeg.model_fields
        assert not {"quantity", "lots", "contracts"} & set(IntentLeg.model_fields)

    def test_signed_ratio_encodes_direction(self) -> None:
        assert f.leg(side=Side.BUY, ratio=2).signed_ratio == 2
        assert f.leg(side=Side.SELL, ratio=2).signed_ratio == -2

    def test_at_least_one_leg_is_required(self) -> None:
        with pytest.raises(ValidationError, match="at least one leg"):
            f.intent(legs=())

    def test_duplicate_leg_ids_are_rejected(self) -> None:
        """The idempotency key derives from leg_id, so duplicates collapse orders."""
        with pytest.raises(ValidationError, match="must be unique"):
            f.intent(legs=(f.leg("leg-1"), f.leg("leg-1")))

    def test_legs_must_share_the_declared_underlying(self) -> None:
        other = f.option_contract(symbol="BANKNIFTY26SEP", underlying="BANKNIFTY")
        with pytest.raises(ValidationError, match="other underlyings"):
            f.intent(
                legs=(
                    f.leg(),
                    IntentLeg(leg_id="leg-2", contract=other, side=Side.SELL, ratio=1),
                )
            )

    def test_max_loss_must_be_defined_and_positive(self) -> None:
        with pytest.raises(ValidationError, match="must be positive and defined"):
            f.intent(estimated_max_loss=f.money("0"))

    def test_max_loss_below_requested_risk_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="risks less than it asks for"):
            f.intent(
                requested_risk=f.money("10000"), estimated_max_loss=f.money("5000")
            )

    def test_expiry_must_follow_creation(self) -> None:
        with pytest.raises(ValidationError, match="expires_at must be after"):
            f.intent(expires_at=f.NOW)

    def test_expired_intent_is_not_live(self) -> None:
        assert f.intent().is_live_at(f.NOW)
        assert not f.intent().is_live_at(f.LATER)

    def test_intent_cannot_supersede_itself(self) -> None:
        with pytest.raises(ValidationError, match="cannot supersede itself"):
            f.intent(supersedes_intent_id="INT-1")

    def test_a_revision_links_to_what_it_replaces(self) -> None:
        revised = f.intent(intent_id="INT-2", supersedes_intent_id="INT-1")
        assert revised.supersedes_intent_id == "INT-1"
        assert revised.intent_id != "INT-1"

    def test_zero_tolerance_entry_policy_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="must be positive"):
            f.entry_policy(max_slippage=Percent.from_bps("0"))


class TestExitTemplateMonotonicity:
    """Invariant 17: stops never widen after entry."""

    def test_a_trail_wider_than_the_initial_stop_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="may only tighten"):
            f.exit_template(
                stop_distance_ticks=100,
                trailing_activation_ticks=50,
                trailing_distance_ticks=200,
            )

    def test_a_trail_equal_to_the_initial_stop_is_allowed(self) -> None:
        template = f.exit_template(
            stop_distance_ticks=100,
            trailing_activation_ticks=50,
            trailing_distance_ticks=100,
        )
        assert template.trailing_distance_ticks == 100

    @given(st.integers(min_value=1, max_value=500))
    def test_any_trail_within_the_initial_stop_is_accepted(self, distance: int) -> None:
        template = f.exit_template(
            stop_distance_ticks=500,
            trailing_activation_ticks=10,
            trailing_distance_ticks=distance,
        )
        assert template.trailing_distance_ticks == distance
        assert distance <= template.stop_distance_ticks

    def test_half_specified_trail_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="needs both"):
            f.exit_template(trailing_activation_ticks=50)

    def test_a_stop_is_always_required(self) -> None:
        with pytest.raises(ValidationError):
            f.exit_template(stop_distance_ticks=0)

    def test_break_even_beyond_the_target_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="could never fire"):
            f.exit_template(target_distance_ticks=100, break_even_trigger_ticks=200)

    def test_there_is_no_field_for_widening_a_stop(self) -> None:

        names = {n.replace("_", "") for n in ExitTemplate.model_fields}
        assert not {"widen", "stopwidening", "loosen", "relaxstop"} & names


class TestRiskDecision:
    def test_approval_requires_a_capital_reservation(self) -> None:
        """Invariant 14: capital is reserved before submission."""
        with pytest.raises(ValidationError, match="requires a capital reservation"):
            f.risk_decision(capital_reservation_id=None, reserved_capital=None)

    def test_approval_requires_layer_two_s_own_max_loss(self) -> None:
        """Invariant 4: the strategy's estimate is not authoritative."""
        with pytest.raises(ValidationError, match="own max-loss"):
            f.risk_decision(recalculated_max_loss=None)

    def test_approval_requires_a_post_fill_projection(self) -> None:
        with pytest.raises(ValidationError, match="projected post-fill state"):
            f.risk_decision(post_trade_projection=None)

    @pytest.mark.parametrize("action", [RiskAction.REJECT, RiskAction.DEFER])
    def test_refusal_must_not_hold_capital(self, action: RiskAction) -> None:
        """Unreleased capital on a refused intent silently shrinks margin."""
        with pytest.raises(
            ValidationError, match="must not hold a capital reservation"
        ):
            f.risk_decision(
                action=action,
                approved_legs=(),
                recalculated_max_loss=None,
                margin_required=None,
                post_trade_projection=None,
                reason_codes=(ReasonCode.RISK_LIMIT_PORTFOLIO,),
            )

    @pytest.mark.parametrize("action", [RiskAction.REJECT, RiskAction.DEFER])
    def test_refusal_must_not_approve_a_leg(self, action: RiskAction) -> None:
        with pytest.raises(ValidationError, match="must not approve any leg"):
            f.risk_decision(
                action=action,
                capital_reservation_id=None,
                reserved_capital=None,
                recalculated_max_loss=None,
                margin_required=None,
                post_trade_projection=None,
                reason_codes=(ReasonCode.MARGIN_INSUFFICIENT,),
            )

    def test_refusal_cannot_be_explained_by_ok_alone(self) -> None:
        with pytest.raises(ValidationError, match="cannot be explained by OK"):
            f.risk_decision(
                action=RiskAction.REJECT,
                approved_legs=(),
                capital_reservation_id=None,
                reserved_capital=None,
                recalculated_max_loss=None,
                margin_required=None,
                post_trade_projection=None,
                reason_codes=(ReasonCode.OK,),
            )

    def test_every_decision_carries_a_reason_code(self) -> None:
        with pytest.raises(ValidationError, match="at least one reason code"):
            f.risk_decision(reason_codes=())

    def test_expired_approval_is_not_valid(self) -> None:
        """An expired approval must be recalculated, never consumed."""
        decision = f.risk_decision()
        assert decision.is_valid_at(f.NOW)
        assert not decision.is_valid_at(f.NOW + timedelta(hours=1))

    def test_a_refusal_never_permits_submission(self) -> None:
        refusal = f.risk_decision(
            action=RiskAction.REJECT,
            approved_legs=(),
            capital_reservation_id=None,
            reserved_capital=None,
            recalculated_max_loss=None,
            margin_required=None,
            post_trade_projection=None,
            reason_codes=(ReasonCode.ENTRY_FROZEN,),
        )
        assert not refusal.permits_submission
        assert not refusal.is_valid_at(f.NOW)

    def test_zero_lot_approval_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="zero lots"):
            f.risk_decision(approved_legs=(f.approved_leg(lots=0),))

    def test_approved_quantity_is_derived_from_lots_and_lot_size(self) -> None:
        assert f.approved_leg(lots=2).quantity.contracts == 150

    def test_exposure_cannot_mix_currencies(self) -> None:

        with pytest.raises(ValidationError, match="mixes currencies"):
            f.exposure(equity=Money.of("700000", Currency.USD))


class TestOrderEvent:
    def test_a_timeout_state_cannot_report_a_fill(self) -> None:
        """Invariant 12."""
        with pytest.raises(ValidationError, match="invariant 12"):
            f.order_event(
                state=OrderState.UNKNOWN,
                filled_quantity=75,
                average_fill_price=f.price("120.00"),
                reason_code=ReasonCode.ORDER_TIMEOUT,
            )

    @pytest.mark.parametrize(
        "state",
        [
            OrderState.CREATED,
            OrderState.SUBMITTING,
            OrderState.ACKNOWLEDGED,
            OrderState.CANCEL_PENDING,
            OrderState.CANCELLED,
            OrderState.REJECTED,
            OrderState.EXPIRED,
            OrderState.UNKNOWN,
        ],
    )
    def test_only_partial_and_filled_may_carry_a_fill(self, state: OrderState) -> None:
        with pytest.raises(ValidationError):
            f.order_event(
                state=state,
                filled_quantity=10,
                average_fill_price=f.price("120.00"),
                reason_code=ReasonCode.BROKER_REJECTED,
            )

    def test_filled_requires_the_full_quantity(self) -> None:
        with pytest.raises(ValidationError, match="FILLED requires the full"):
            f.order_event(
                state=OrderState.FILLED,
                filled_quantity=50,
                average_fill_price=f.price("120.00"),
            )

    def test_partial_requires_a_strictly_partial_fill(self) -> None:
        with pytest.raises(ValidationError, match="strictly between"):
            f.order_event(
                state=OrderState.PARTIAL,
                filled_quantity=75,
                average_fill_price=f.price("120.00"),
            )

    def test_a_valid_partial_fill_is_accepted(self) -> None:
        event = f.order_event(
            state=OrderState.PARTIAL,
            filled_quantity=25,
            average_fill_price=f.price("120.00"),
        )
        assert event.outstanding_quantity == 50

    def test_fill_beyond_the_requested_quantity_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="exceeds requested"):
            f.order_event(
                state=OrderState.FILLED,
                filled_quantity=100,
                average_fill_price=f.price("120.00"),
            )

    def test_a_fill_must_record_its_price(self) -> None:
        with pytest.raises(ValidationError, match="must record the price"):
            f.order_event(state=OrderState.PARTIAL, filled_quantity=25)

    def test_a_price_without_a_fill_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="without a fill is meaningless"):
            f.order_event(average_fill_price=f.price("120.00"))

    @pytest.mark.parametrize(
        "state", [OrderState.REJECTED, OrderState.EXPIRED, OrderState.UNKNOWN]
    )
    def test_failure_states_require_a_machine_readable_reason(
        self, state: OrderState
    ) -> None:
        with pytest.raises(ValidationError, match="requires a machine-readable reason"):
            f.order_event(state=state, reason_code=None)

    def test_idempotency_key_is_mandatory(self) -> None:
        """Invariant 11: every attempt carries the key."""

        assert OrderIdentity.model_fields["idempotency_key"].is_required()

    def test_attempt_number_is_separate_from_the_key(self) -> None:
        """Retries share a key and differ only in attempt_number."""
        first = f.order_event(attempt_number=1)
        second = f.order_event(attempt_number=2)
        assert first.identity.idempotency_key == second.identity.idempotency_key
        assert first.attempt_number != second.attempt_number

    def test_a_working_order_needs_a_broker_id_for_reconciliation(self) -> None:
        with pytest.raises(ValidationError, match="broker_order_id is required"):
            f.order_event(identity=f.order_identity(broker_order_id=None))

    def test_submitting_may_precede_a_broker_id(self) -> None:
        event = f.order_event(
            state=OrderState.SUBMITTING,
            acknowledged_quantity=0,
            identity=f.order_identity(broker_order_id=None),
        )
        assert event.identity.broker_order_id is None

    def test_limit_order_without_a_price_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="requires a limit price"):
            f.order_command(order_type=OrderType.LIMIT, limit_price=None)

    def test_market_order_with_a_limit_price_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="must not carry a limit price"):
            f.order_command(order_type=OrderType.MARKET)

    def test_received_before_sent_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="precedes sent_at"):
            f.order_event(received_at=f.NOW - timedelta(seconds=1))


class TestReconciliationEvent:
    def test_a_matched_comparison_still_produces_a_record(self) -> None:
        """Invariant 25: every recovery transition is auditable."""
        assert f.reconciliation_event().is_resolved

    def test_a_matched_comparison_cannot_be_critical(self) -> None:
        with pytest.raises(ValidationError, match="cannot be above INFO"):
            f.reconciliation_event(severity=Severity.CRITICAL)

    def test_a_difference_cannot_be_explained_by_ok(self) -> None:
        with pytest.raises(ValidationError, match="cannot be explained by OK"):
            f.reconciliation_event(
                difference_class=DifferenceClass.TIMING_LAG,
                severity=Severity.WARNING,
                entries_blocked=True,
            )

    def test_a_critical_break_requires_a_repair_action(self) -> None:
        with pytest.raises(ValidationError, match="requires a deterministic repair"):
            f.reconciliation_event(
                difference_class=DifferenceClass.UNEXPECTED_BROKER_STATE,
                severity=Severity.CRITICAL,
                reason_code=ReasonCode.RECONCILIATION_UNRESOLVED,
                entries_blocked=True,
                resolved_at=None,
            )

    def test_an_unrepaired_break_must_block_entries(self) -> None:
        with pytest.raises(ValidationError, match="entries must stay blocked"):
            f.reconciliation_event(
                difference_class=DifferenceClass.MISSING_LOCAL_EVENT,
                severity=Severity.WARNING,
                reason_code=ReasonCode.RECONCILIATION_UNRESOLVED,
                repair_action="import broker evidence",
                repair_succeeded=None,
                entries_blocked=False,
                resolved_at=None,
            )

    def test_a_failed_repair_is_not_resolved(self) -> None:
        with pytest.raises(ValidationError, match="not resolved"):
            f.reconciliation_event(
                difference_class=DifferenceClass.MAPPING_ISSUE,
                severity=Severity.WARNING,
                reason_code=ReasonCode.SNAPSHOT_MISMATCH,
                repair_action="rebuild local representation",
                repair_succeeded=False,
                entries_blocked=True,
                resolved_at=f.NOW,
            )

    def test_a_successful_repair_records_when_it_resolved(self) -> None:
        with pytest.raises(ValidationError, match="must record resolved_at"):
            f.reconciliation_event(
                difference_class=DifferenceClass.MAPPING_ISSUE,
                severity=Severity.WARNING,
                reason_code=ReasonCode.SNAPSHOT_MISMATCH,
                repair_action="rebuild local representation",
                repair_succeeded=True,
                entries_blocked=True,
                resolved_at=None,
            )

    def test_a_repair_outcome_without_an_action_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="without a repair action"):
            f.reconciliation_event(repair_succeeded=True)
