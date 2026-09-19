"""Layer 4 forward-validation contracts. Advisory evidence, never live mutation.

Invariant 22: an eligibility result or scorecard cannot deploy configuration.
Invariant 2: these types carry no broker, order or promotion levers.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import Field, model_validator

from trading.domain.contracts.base import (
    ExactDecimal,
    NonEmptyStr,
    StrictBool,
    StrictInt,
    StrictModel,
    UtcDatetime,
    VersionedModel,
)
from trading.domain.contracts.identification import RouteDecision, SetupFeatures
from trading.domain.contracts.intent import TradeIntent
from trading.domain.contracts.order import OrderCommand
from trading.domain.contracts.snapshot import MarketQuote
from trading.domain.enums import (
    EligibilityStatus,
    ExecutionMode,
    FillOutcome,
    ReasonCode,
    RiskAction,
    Severity,
)
from trading.domain.primitives import Money, Price

__all__ = [
    "CohortPackage",
    "CohortScorecard",
    "CohortSignal",
    "ExperimentDefinition",
    "FillSimulation",
    "JudgmentReport",
    "JudgmentSignalResult",
    "PromotionEligibilityResult",
    "ReasonCount",
]


class ExperimentDefinition(VersionedModel):
    """Frozen identity of one forward-validation run."""

    experiment_id: NonEmptyStr
    strategy_id: NonEmptyStr
    strategy_version: NonEmptyStr
    parameter_version: NonEmptyStr
    execution_mode: ExecutionMode
    started_at: UtcDatetime
    capital_limit: Money
    parameters_frozen: StrictBool = True
    feature_set_version: NonEmptyStr
    risk_policy_version: NonEmptyStr
    fill_model_version: NonEmptyStr
    code_version: NonEmptyStr
    identification_rule_version: NonEmptyStr = "legacy"
    router_version: NonEmptyStr = "legacy"

    @model_validator(mode="after")
    def _capital_limit_is_non_negative(self) -> ExperimentDefinition:
        if not self.parameters_frozen:
            raise ValueError(
                "experiment is frozen after start; allocate a new "
                "experiment_id to change parameters"
            )
        if self.capital_limit.is_negative:
            raise ValueError("capital_limit must not be negative")
        if self.execution_mode is ExecutionMode.SHADOW and (
            not self.capital_limit.is_zero
        ):
            raise ValueError("SHADOW experiments must have a zero capital_limit")
        if (
            self.execution_mode.touches_real_capital
            and self.execution_mode is not ExecutionMode.SUSPENDED
            and self.capital_limit.is_zero
        ):
            raise ValueError(
                f"{self.execution_mode} requires a positive capital_limit; "
                "zero capital is SHADOW or SUSPENDED"
            )
        return self


class FillSimulation(StrictModel):
    """Conservative fill reconstruction. Not a broker-confirmed OrderEvent."""

    outcome: FillOutcome
    filled_quantity: StrictInt = Field(ge=0)
    intended_price: Price
    fill_price: Price | None = None
    slippage: Money | None = None
    charges: Money | None = None
    charges_confirmed: StrictBool = False
    reason_code: ReasonCode
    reason_detail: str = ""

    @model_validator(mode="after")
    def _outcome_matches_quantity(self) -> FillSimulation:
        if self.outcome is FillOutcome.FILLED and (
            self.filled_quantity <= 0 or self.fill_price is None
        ):
            raise ValueError("FILLED requires a positive quantity and fill price")
        if self.outcome is FillOutcome.PARTIAL and (
            self.filled_quantity <= 0 or self.fill_price is None
        ):
            raise ValueError("PARTIAL requires a positive quantity and fill price")
        if self.outcome in {FillOutcome.UNFILLED, FillOutcome.REJECTED} and (
            self.filled_quantity != 0 or self.fill_price is not None
        ):
            raise ValueError(f"{self.outcome} must not claim a fill")
        if self.charges is not None and not self.charges_confirmed:
            raise ValueError("unconfirmed charges must not carry a money amount")
        if self.charges_confirmed and self.charges is None:
            raise ValueError("confirmed charges must record the amount, including zero")
        return self


class CohortSignal(StrictModel):
    """One decision in an evidence cohort, including declines and rejects."""

    signal_id: NonEmptyStr
    snapshot_id: NonEmptyStr
    created_at: UtcDatetime
    declined: StrictBool = False
    rejection_reason: ReasonCode | None = None
    intent: TradeIntent | None = None
    risk_action: RiskAction | None = None
    risk_reasons: tuple[ReasonCode, ...] = ()
    entry_quote: MarketQuote | None = None
    decision_quotes: dict[NonEmptyStr, MarketQuote] = Field(default_factory=dict)
    exit_quote: MarketQuote | None = None
    entry_command: OrderCommand | None = None
    exit_command: OrderCommand | None = None
    traded_through_entry: StrictBool = False
    traded_through_exit: StrictBool = False
    mae: Money | None = None
    mfe: Money | None = None
    regime: NonEmptyStr | None = None
    days_to_expiry: StrictInt | None = Field(default=None, ge=0)
    session_label: NonEmptyStr | None = None
    incident_severity: Severity | None = None
    reconciled: StrictBool = True
    lots: StrictInt = Field(default=1, gt=0)
    setup_features: SetupFeatures | None = None
    route_decision: RouteDecision | None = None
    executed: StrictBool = True
    judgment_label: StrictBool | None = None

    @model_validator(mode="after")
    def _decline_and_intent_agree(self) -> CohortSignal:
        if self.declined and self.intent is not None:
            raise ValueError("a declined signal must not carry an intent")
        if self.declined and self.rejection_reason is None:
            raise ValueError("a declined signal requires a rejection_reason")
        if not self.declined and self.intent is None:
            raise ValueError("an emitted signal requires an intent")
        return self


class CohortPackage(VersionedModel):
    """Immutable input package for one Layer 4 evaluation job."""

    experiment: ExperimentDefinition
    signals: tuple[CohortSignal, ...]
    observation_start: UtcDatetime
    observation_end: UtcDatetime

    @model_validator(mode="after")
    def _window_and_cohort_agree(self) -> CohortPackage:
        if self.observation_end < self.observation_start:
            raise ValueError("observation_end precedes observation_start")
        if not self.experiment.parameters_frozen:
            raise ValueError(
                "an unfrozen experiment cannot be scored; freeze parameters "
                "for the evaluation window or start a new experiment_id"
            )
        ids = [signal.signal_id for signal in self.signals]
        if len(set(ids)) != len(ids):
            raise ValueError("cohort signal_id values must be unique")
        for signal in self.signals:
            intent = signal.intent
            if intent is None:
                continue
            if intent.experiment_id != self.experiment.experiment_id:
                raise ValueError(
                    f"signal {signal.signal_id} experiment_id "
                    f"{intent.experiment_id} does not match the package"
                )
            if intent.execution_mode is not self.experiment.execution_mode:
                raise ValueError(
                    f"signal {signal.signal_id} execution_mode does not match "
                    "the experiment"
                )
        return self


class ReasonCount(StrictModel):
    """One risk-gateway reason and how often it occurred."""

    reason_code: ReasonCode
    count: StrictInt = Field(ge=0)


class CohortScorecard(VersionedModel):
    """Deterministic metrics for one frozen experiment and fill-model version."""

    scorecard_id: NonEmptyStr
    experiment_id: NonEmptyStr
    fill_model_version: NonEmptyStr
    as_of: UtcDatetime
    signal_count: StrictInt = Field(ge=0)
    declined_count: StrictInt = Field(ge=0)
    closed_trade_count: StrictInt = Field(ge=0)
    filled_count: StrictInt = Field(ge=0)
    partial_count: StrictInt = Field(ge=0)
    unfilled_count: StrictInt = Field(ge=0)
    reject_count: StrictInt = Field(ge=0)
    gross_pnl: Money
    net_pnl: Money | None = None
    costs_confirmed: StrictBool
    expectancy: Money | None = None
    expectancy_lower_95: Money | None = None
    win_count: StrictInt = Field(ge=0)
    loss_count: StrictInt = Field(ge=0)
    average_win: Money | None = None
    average_loss: Money | None = None
    profit_factor: ExactDecimal | None = None
    max_drawdown: Money
    consecutive_losses: StrictInt = Field(ge=0)
    fill_rate: ExactDecimal = Field(ge=0, le=1)
    partial_fill_rate: ExactDecimal = Field(ge=0, le=1)
    reject_rate: ExactDecimal = Field(ge=0, le=1)
    average_entry_slippage: Money | None = None
    average_mae: Money | None = None
    average_mfe: Money | None = None
    reason_histogram: tuple[ReasonCount, ...] = ()
    regime_count: StrictInt = Field(ge=0)
    max_single_trade_pnl_share: ExactDecimal | None = Field(default=None, ge=0, le=1)
    incident_p0_p1_count: StrictInt = Field(ge=0)
    unreconciled_count: StrictInt = Field(ge=0)
    observation_days: StrictInt = Field(ge=0)
    setup_feature_count: StrictInt = Field(default=0, ge=0)
    raw_score_count: StrictInt = Field(default=0, ge=0)
    calibrated_probability_count: StrictInt = Field(default=0, ge=0)
    setup_coverage: ExactDecimal = Field(default=Decimal(0), ge=0, le=1)
    abstention_rate: ExactDecimal = Field(default=Decimal(0), ge=0, le=1)
    brier_score: ExactDecimal | None = Field(default=None, ge=0, le=1)


class PromotionEligibilityResult(VersionedModel):
    """Fail-closed eligibility. Contains no deploy or config-write fields."""

    result_id: NonEmptyStr
    experiment_id: NonEmptyStr
    scorecard_id: NonEmptyStr
    status: EligibilityStatus
    evaluated_at: UtcDatetime
    failed_gates: tuple[NonEmptyStr, ...] = ()
    threshold_checksum: NonEmptyStr
    detail: str = ""


class JudgmentSignalResult(StrictModel):
    """Per-signal offline label versus whether the desk entered."""

    signal_id: NonEmptyStr
    should_enter: StrictBool | None = None
    entered: StrictBool
    confidence: ExactDecimal | None = Field(default=None, ge=0, le=1)
    mae: Money | None = None
    mfe: Money | None = None
    charges: Money | None = None
    net_mfe: Money | None = None


class JudgmentReport(VersionedModel):
    """Cohort judgment metrics: precision, capture, optional Brier."""

    experiment_id: NonEmptyStr
    as_of: UtcDatetime
    fill_model_version: NonEmptyStr
    labeled_count: StrictInt = Field(ge=0)
    should_enter_count: StrictInt = Field(ge=0)
    should_pass_count: StrictInt = Field(ge=0)
    entered_count: StrictInt = Field(ge=0)
    true_positive_count: StrictInt = Field(ge=0)
    false_positive_count: StrictInt = Field(ge=0)
    false_negative_count: StrictInt = Field(ge=0)
    precision: ExactDecimal | None = Field(default=None, ge=0, le=1)
    capture: ExactDecimal | None = Field(default=None, ge=0, le=1)
    brier_score: ExactDecimal | None = Field(default=None, ge=0, le=1)
    failed_gate_ids: tuple[NonEmptyStr, ...] = ()
    signals: tuple[JudgmentSignalResult, ...] = ()
