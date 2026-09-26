"""Closed enumerations shared by every contract and state machine.

Every set here is deliberately closed. A free-text status or reason would leak
into dashboards, alert routing and promotion gates, where a typo becomes an
unmonitored failure mode. Adding a member is a contract change.
"""

from __future__ import annotations

from enum import StrEnum, unique

__all__ = [
    "ADVISORY_ACTIONS",
    "BOUNDED_ACTIONS",
    "CONFIG_PROMOTION_ACTIONS",
    "LIVE_PATH_ACTIONS",
    "AgentAction",
    "AgentReasonCode",
    "AssetClass",
    "AttentionBlocker",
    "AttributionCode",
    "AuthorityMode",
    "CarryGateAction",
    "Comparator",
    "ConfidenceBucket",
    "DataQuality",
    "DemotionReason",
    "DiscoveryDecisionKind",
    "DiscoveryStage",
    "DeskRole",
    "DifferenceClass",
    "DirectionalClaim",
    "DriverCode",
    "EligibilityStatus",
    "EntryGateId",
    "EntryProfile",
    "Environment",
    "EventClass",
    "Exchange",
    "ExecutionMode",
    "ExitScope",
    "FamilyId",
    "FamilyResearchStatus",
    "FamilyStance",
    "FillOutcome",
    "FunnelStage",
    "GateOutcome",
    "HoldingStyle",
    "ImprovementArea",
    "ImprovementStatus",
    "InstrumentKind",
    "IntentState",
    "InvalidationMetric",
    "InvalidationSeverity",
    "InvalidationStatus",
    "LiquidityGrade",
    "MacroEventSeverity",
    "ModeId",
    "OptionType",
    "OrderPlanState",
    "OrderState",
    "OrderType",
    "PlaybookEditKind",
    "PlaybookTriggerKind",
    "ProposalType",
    "ProtectionStatus",
    "QuoteMonitorSource",
    "ReadinessLevel",
    "ReasonCode",
    "Recommendation",
    "ReconciliationTrigger",
    "ReservationState",
    "ReviewAction",
    "ReviewExecutionStatus",
    "ReviewSlotId",
    "RiskAction",
    "Severity",
    "SharedFateCode",
    "Side",
    "SizingBindingConstraint",
    "SystemState",
    "TerminalPolicyKind",
    "TerminalPolicyRejectReason",
    "TestabilityKind",
    "ThesisVerdict",
    "TimeInForce",
    "TradeState",
    "Trigger",
    "VetoCode",
]


@unique
class Exchange(StrEnum):
    NSE = "NSE"
    NFO = "NFO"
    MCX = "MCX"


@unique
class AssetClass(StrEnum):
    EQUITY_INDEX = "EQUITY_INDEX"
    EQUITY_STOCK = "EQUITY_STOCK"
    COMMODITY = "COMMODITY"


@unique
class InstrumentKind(StrEnum):
    """Determines the pricing convention: spot-based versus futures-based."""

    INDEX = "INDEX"
    STOCK = "STOCK"
    FUTURE = "FUTURE"
    OPTION = "OPTION"


@unique
class OptionType(StrEnum):
    CALL = "CALL"
    PUT = "PUT"


@unique
class Environment(StrEnum):
    """Process-level environment. Distinct from per-trade ExecutionMode."""

    BACKTEST = "BACKTEST"
    PAPER = "PAPER"
    LIVE = "LIVE"

    @property
    def touches_real_capital(self) -> bool:
        return self is Environment.LIVE


@unique
class EntryProfile(StrEnum):
    """Entry profile: STRICT (default) or temporary PAPER DISCOVERY."""

    STRICT = "STRICT"
    DISCOVERY = "DISCOVERY"


@unique
class DiscoveryDecisionKind(StrEnum):
    """Outcome class for a durable decision record."""

    TRADE = "TRADE"
    NO_TRADE = "NO_TRADE"
    BLOCKED_HARD = "BLOCKED_HARD"


@unique
class DiscoveryStage(StrEnum):
    """Pipeline stage where a decision record was emitted."""

    DATA = "DATA"
    DIRECTION = "DIRECTION"
    BIND = "BIND"
    STRATEGY = "STRATEGY"
    ARBITER = "ARBITER"
    RISK = "RISK"
    FILL = "FILL"
    EXIT = "EXIT"


@unique
class ExecutionMode(StrEnum):
    """Per-trade execution stage. Distinct from process Environment."""

    SHADOW = "SHADOW"
    PAPER = "PAPER"
    CANARY_REAL = "CANARY_REAL"
    LIMITED_REAL = "LIMITED_REAL"
    NORMAL_REAL = "NORMAL_REAL"
    SUSPENDED = "SUSPENDED"

    @property
    def touches_real_capital(self) -> bool:
        return self in {
            ExecutionMode.CANARY_REAL,
            ExecutionMode.LIMITED_REAL,
            ExecutionMode.NORMAL_REAL,
        }


@unique
class ModeId(StrEnum):
    """Four named trading modes sharing the portfolio arbiter (P1/spec §4)."""

    M1_CAS = "M1_CAS"
    M2_DIRECTIONAL = "M2_DIRECTIONAL"
    M3_TACTICAL_POSITIONAL = "M3_TACTICAL_POSITIONAL"
    M4_STRATEGIC_POSITIONAL = "M4_STRATEGIC_POSITIONAL"


@unique
class FamilyId(StrEnum):
    """The 14 defined strategy families (§5 of NIFTY_FOUR_MODE_CURSOR_REDESIGN.md)."""

    long_call = "long_call"
    long_put = "long_put"
    bull_call_debit = "bull_call_debit"
    bear_put_debit = "bear_put_debit"
    bull_put_credit = "bull_put_credit"
    bear_call_credit = "bear_call_credit"
    short_iron_condor_defined = "short_iron_condor_defined"
    short_iron_butterfly_defined = "short_iron_butterfly_defined"
    long_call_butterfly = "long_call_butterfly"
    long_put_butterfly = "long_put_butterfly"
    long_straddle = "long_straddle"
    long_strangle = "long_strangle"
    long_call_calendar = "long_call_calendar"
    long_put_calendar = "long_put_calendar"


@unique
class FillOutcome(StrEnum):
    """Conservative paper-fill calculator result. Not a broker fill."""

    FILLED = "FILLED"
    PARTIAL = "PARTIAL"
    UNFILLED = "UNFILLED"
    REJECTED = "REJECTED"


@unique
class EligibilityStatus(StrEnum):
    """Deterministic promotion eligibility. Never deploys configuration."""

    ELIGIBLE = "ELIGIBLE"
    INELIGIBLE = "INELIGIBLE"
    INSUFFICIENT_SAMPLE = "INSUFFICIENT_SAMPLE"


@unique
class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


@unique
class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"
    STOP_LIMIT = "STOP_LIMIT"


@unique
class TimeInForce(StrEnum):
    DAY = "DAY"
    IOC = "IOC"


@unique
class DataQuality(StrEnum):
    """DATA_SPEC.md quality gate. Thresholds are per strategy, per timeframe."""

    VALID = "VALID"
    DEGRADED = "DEGRADED"
    STALE = "STALE"
    INVALID = "INVALID"

    @property
    def blocks_new_exposure(self) -> bool:
        """Invariant 6: unknown, stale or invalid state blocks new exposure."""
        return self in {DataQuality.STALE, DataQuality.INVALID}


@unique
class ReadinessLevel(StrEnum):
    """OPERATIONS_RUNBOOK.md readiness levels, evaluated independently."""

    LIVE_SAFE = "LIVE_SAFE"
    ENTRY_READY = "ENTRY_READY"
    RESEARCH_READY = "RESEARCH_READY"


@unique
class SystemState(StrEnum):
    STARTING = "STARTING"
    RECOVERY = "RECOVERY"
    READY = "READY"
    DEGRADED = "DEGRADED"
    HALTED = "HALTED"

    @property
    def permits_new_exposure(self) -> bool:
        """Only READY admits entries. Invariant 9 keeps RECOVERY closed."""
        return self is SystemState.READY


@unique
class IntentState(StrEnum):
    CREATED = "CREATED"
    VALIDATED = "VALIDATED"
    APPROVED = "APPROVED"
    RESIZED = "RESIZED"
    DEFERRED = "DEFERRED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


@unique
class RiskAction(StrEnum):
    """The only outputs Layer 2 may return for a Trade Intent."""

    APPROVE = "APPROVE"
    RESIZE = "RESIZE"
    DEFER = "DEFER"
    REJECT = "REJECT"


@unique
class ReservationState(StrEnum):
    """Capital reservation lifecycle. Invariant 14."""

    REQUESTED = "REQUESTED"
    RESERVED = "RESERVED"
    REJECTED = "REJECTED"
    COMMITTED = "COMMITTED"
    RELEASED = "RELEASED"

    @property
    def holds_capital(self) -> bool:
        return self in {ReservationState.RESERVED, ReservationState.COMMITTED}


@unique
class SizingBindingConstraint(StrEnum):
    """Which term bound the final lot count in the min() sizing formula."""

    RISK = "RISK"
    CAPITAL = "CAPITAL"
    MARGIN = "MARGIN"
    PORTFOLIO_LIMIT = "PORTFOLIO_LIMIT"
    LIQUIDITY = "LIQUIDITY"


@unique
class ExitScope(StrEnum):
    """What price or P&L series a stop or target applies to."""

    STRATEGY_PNL = "STRATEGY_PNL"
    LEG_PRICE = "LEG_PRICE"
    UNDERLYING = "UNDERLYING"
    SPREAD_VALUE = "SPREAD_VALUE"


@unique
class CarryGateAction(StrEnum):
    """Mode 2 overnight carry gate outcome recorded before entry cutoff."""

    CARRY_APPROVED = "CARRY_APPROVED"
    CARRY_REJECTED = "CARRY_REJECTED"


@unique
class HoldingStyle(StrEnum):
    """Whether an open trade is held overnight or closed the same session."""

    POSITIONAL = "POSITIONAL"
    INTRADAY = "INTRADAY"


@unique
class ReviewAction(StrEnum):
    """Deterministic twice-daily positional review outcome. Never an AI rewrite."""

    HOLD = "HOLD"
    TIGHTEN_STOP = "TIGHTEN_STOP"
    PARTIAL_EXIT = "PARTIAL_EXIT"
    FULL_EXIT = "FULL_EXIT"
    PROPOSE_HEDGE = "PROPOSE_HEDGE"
    PROPOSE_ROLL = "PROPOSE_ROLL"
    PROPOSE_SWITCH = "PROPOSE_SWITCH"
    ROLL = "ROLL"
    SWITCH = "SWITCH"

    @property
    def is_proposal(self) -> bool:
        """HEDGE/ROLL/SWITCH proposals never auto-submit without G2 close/open."""
        return self in {
            ReviewAction.PROPOSE_HEDGE,
            ReviewAction.PROPOSE_ROLL,
            ReviewAction.PROPOSE_SWITCH,
        }

    @property
    def submits_exit(self) -> bool:
        return self in {
            ReviewAction.PARTIAL_EXIT,
            ReviewAction.FULL_EXIT,
            ReviewAction.ROLL,
            ReviewAction.SWITCH,
        }


@unique
class ReviewExecutionStatus(StrEnum):
    """Whether a review action with a linked trade was executed."""

    EXECUTED = "EXECUTED"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    PROPOSED_NOT_EXECUTED = "PROPOSED_NOT_EXECUTED"
    CLOSE_SUBMITTED = "CLOSE_SUBMITTED"
    REPLACEMENT_PENDING_L2 = "REPLACEMENT_PENDING_L2"
    REPLACEMENT_REJECTED = "REPLACEMENT_REJECTED"
    ROLL_SWITCH_COMPLETE = "ROLL_SWITCH_COMPLETE"


@unique
class RollSwitchStatus(StrEnum):
    """Lifecycle of one linked roll/switch close-then-replace transaction."""

    CLOSE_PENDING = "CLOSE_PENDING"
    CLOSE_COMPLETE = "CLOSE_COMPLETE"
    REPLACEMENT_PENDING_L2 = "REPLACEMENT_PENDING_L2"
    REPLACEMENT_REJECTED = "REPLACEMENT_REJECTED"
    COMPLETE = "COMPLETE"


@unique
class ReviewSlotId(StrEnum):
    """Named review slots. NSE is the required path; MCX is config-ready."""

    NSE_MORNING = "NSE_MORNING"
    NSE_AFTERNOON = "NSE_AFTERNOON"
    MCX_MORNING = "MCX_MORNING"
    MCX_AFTERNOON = "MCX_AFTERNOON"

    @property
    def venue(self) -> Exchange:
        if self in {ReviewSlotId.MCX_MORNING, ReviewSlotId.MCX_AFTERNOON}:
            return Exchange.MCX
        return Exchange.NSE


@unique
class OrderPlanState(StrEnum):
    """Logical plan state before per-leg OrderState takes over."""

    CREATED = "CREATED"
    RISK_APPROVED = "RISK_APPROVED"
    SUBMITTED = "SUBMITTED"


@unique
class OrderState(StrEnum):
    CREATED = "CREATED"
    SUBMITTING = "SUBMITTING"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCEL_PENDING = "CANCEL_PENDING"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"

    @property
    def is_terminal(self) -> bool:
        return self in {
            OrderState.FILLED,
            OrderState.REJECTED,
            OrderState.CANCELLED,
            OrderState.EXPIRED,
        }

    @property
    def is_working(self) -> bool:
        """States in which the broker may still fill the order."""
        return self in {
            OrderState.SUBMITTING,
            OrderState.ACKNOWLEDGED,
            OrderState.PARTIAL,
            OrderState.CANCEL_PENDING,
            OrderState.UNKNOWN,
        }


@unique
class TradeState(StrEnum):
    PENDING_ENTRY = "PENDING_ENTRY"
    OPENING = "OPENING"
    OPEN = "OPEN"
    EXIT_PENDING = "EXIT_PENDING"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"
    REPAIR_REQUIRED = "REPAIR_REQUIRED"

    @property
    def requires_protective_coverage(self) -> bool:
        """Invariant 16: every open position maps to active protection."""
        return self in {
            TradeState.OPENING,
            TradeState.OPEN,
            TradeState.EXIT_PENDING,
            TradeState.CLOSING,
            TradeState.REPAIR_REQUIRED,
        }


@unique
class Trigger(StrEnum):
    """What caused a state transition. Guards which edges are legal."""

    STRATEGY = "STRATEGY"
    RISK_DECISION = "RISK_DECISION"
    LOCAL_COMMAND = "LOCAL_COMMAND"
    BROKER_EVENT = "BROKER_EVENT"
    RECONCILIATION = "RECONCILIATION"
    TIMEOUT = "TIMEOUT"
    SCHEDULER = "SCHEDULER"
    OPERATOR = "OPERATOR"
    STARTUP = "STARTUP"


@unique
class ReconciliationTrigger(StrEnum):
    BOOT = "BOOT"
    RECONNECT = "RECONNECT"
    SCHEDULED = "SCHEDULED"
    MANUAL = "MANUAL"
    ANOMALY = "ANOMALY"


@unique
class DifferenceClass(StrEnum):
    """BROKER_SPEC.md discrepancy classification."""

    NONE = "NONE"
    TIMING_LAG = "TIMING_LAG"
    MAPPING_ISSUE = "MAPPING_ISSUE"
    MISSING_LOCAL_EVENT = "MISSING_LOCAL_EVENT"
    UNEXPECTED_BROKER_STATE = "UNEXPECTED_BROKER_STATE"
    LOCAL_ORDER_ABSENT_AT_BROKER = "LOCAL_ORDER_ABSENT_AT_BROKER"


@unique
class Severity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


@unique
class ProposalType(StrEnum):
    MACRO_REGIME = "MACRO_REGIME"
    UNIVERSE_RANKING = "UNIVERSE_RANKING"
    PARAMETER_CANDIDATE = "PARAMETER_CANDIDATE"
    FAILURE_DIAGNOSIS = "FAILURE_DIAGNOSIS"
    STRATEGY_FAMILY = "STRATEGY_FAMILY"


@unique
class FamilyResearchStatus(StrEnum):
    """Lifecycle and research posture for one strategy family."""

    IMPLEMENTED_UNIT = "IMPLEMENTED_UNIT"
    LIFECYCLE_PROVEN = "LIFECYCLE_PROVEN"
    PAPER_STANCE_ENABLED = "PAPER_STANCE_ENABLED"
    EXPERIMENTAL_ONLY_RISK_BOUND_UNPROVEN = "EXPERIMENTAL_ONLY_RISK_BOUND_UNPROVEN"


@unique
class FunnelStage(StrEnum):
    """Stages in the four-mode activity funnel (R-023)."""

    EVALUATION = "evaluation"
    ELIGIBLE_SIGNAL = "eligible_signal"
    BOUND_CONTRACTS = "bound_contracts"
    VALID_STRUCTURE = "valid_structure"
    MODE_RISK = "mode_risk"
    PORTFOLIO = "portfolio"
    ORDER = "order"
    FILL = "fill"
    MANAGED_EXIT = "managed_exit"


@unique
class FamilyStance(StrEnum):
    """Proposed paper/shadow posture for one strategy family. Never a live switch."""

    ENABLE = "ENABLE"
    SHADOW = "SHADOW"
    HALT = "HALT"


@unique
class ProtectionStatus(StrEnum):
    """Software protection monitor state for an open PAPER position."""

    ACTIVE = "ACTIVE"
    DEGRADED = "DEGRADED"
    STOPPED = "STOPPED"


@unique
class QuoteMonitorSource(StrEnum):
    """Where a protection-loop quote observation originated."""

    WEBSOCKET = "WEBSOCKET"
    REST = "REST"
    SCRIPTED = "SCRIPTED"
    ENTRY_POLL = "ENTRY_POLL"


@unique
class AttentionBlocker(StrEnum):
    """Why the advisory loop cannot proceed without an operator artifact."""

    CHARGES_UNVERIFIED = "CHARGES_UNVERIFIED"
    CAS_FEATURES_MISSING = "CAS_FEATURES_MISSING"
    LIVE_CONFIG_UNVERIFIED = "LIVE_CONFIG_UNVERIFIED"
    DATA_GAP = "DATA_GAP"
    AGENT_DISABLED = "AGENT_DISABLED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    PROTECTION_DEGRADED = "PROTECTION_DEGRADED"


@unique
class DeskRole(StrEnum):
    """Agent Desk role. Authority is per-role via AuthorityGrant, never inherent."""

    ENTRY = "ENTRY"
    POSITION = "POSITION"
    PORTFOLIO = "PORTFOLIO"
    MACRO = "MACRO"
    FRAGILITY = "FRAGILITY"
    POSTTRADE = "POSTTRADE"
    RESEARCH = "RESEARCH"


@unique
class AuthorityMode(StrEnum):
    """How far a desk's output may travel. Missing grant demotes to OBSERVE."""

    OBSERVE = "OBSERVE"
    SHADOW = "SHADOW"
    ADVISORY = "ADVISORY"
    BOUNDED = "BOUNDED"


@unique
class ConfidenceBucket(StrEnum):
    """Closed agent confidence set (PART 7.3). Continuous floats are rejected.

    Phase-1 sizing maps each bucket to a downscale-only size_multiplier <= 1.0.
    """

    P10 = "0.1"
    P30 = "0.3"
    P50 = "0.5"
    P70 = "0.7"
    P90 = "0.9"


@unique
class AgentAction(StrEnum):
    """Closed agent output vocabulary. C1: BOUNDED may grant config-promotion only."""

    # Config-promotion — the only BOUNDED-eligible set (C1 2026-09-20).
    # Names follow FamilyStance / STRATEGY_FAMILY proposal vocabulary.
    PROPOSE_FAMILY_ENABLE = "PROPOSE_FAMILY_ENABLE"
    PROPOSE_FAMILY_SHADOW = "PROPOSE_FAMILY_SHADOW"
    PROPOSE_FAMILY_HALT = "PROPOSE_FAMILY_HALT"

    # Live-path — never BOUNDED. Size, stop, submit, veto-as-order, tighten,
    # partial/full exit as order influence, roll, hedge, add.
    VETO_ENTRY = "VETO_ENTRY"
    REDUCE_SIZE = "REDUCE_SIZE"
    TIGHTEN_STOP = "TIGHTEN_STOP"
    PARTIAL_EXIT = "PARTIAL_EXIT"
    FULL_EXIT = "FULL_EXIT"
    SUBMIT_ORDER = "SUBMIT_ORDER"
    PROPOSE_ROLL = "PROPOSE_ROLL"
    PROPOSE_HEDGE = "PROPOSE_HEDGE"
    PROPOSE_SIZE_INCREASE = "PROPOSE_SIZE_INCREASE"
    PROPOSE_ADD = "PROPOSE_ADD"

    # Advisory / journal — logged or operator-facing; not BOUNDED under C1.
    ABSTAIN = "ABSTAIN"
    HOLD = "HOLD"
    RANK_STRUCTURES = "RANK_STRUCTURES"
    SELECT_STRIKE_CANDIDATE = "SELECT_STRIKE_CANDIDATE"
    REQUEST_TERMINAL_POLICY = "REQUEST_TERMINAL_POLICY"
    REQUEST_OPERATOR_ATTENTION = "REQUEST_OPERATOR_ATTENTION"
    RECORD_IMPROVEMENT = "RECORD_IMPROVEMENT"

    @property
    def is_config_promotion(self) -> bool:
        """True when this action only proposes FamilyStance via the L4 path."""
        return self in CONFIG_PROMOTION_ACTIONS

    @property
    def is_live_path(self) -> bool:
        """True when this action could influence size, stops, submits or exits."""
        return self in LIVE_PATH_ACTIONS

    @property
    def is_advisory(self) -> bool:
        """True when the action is logged or operator-facing only."""
        return self in ADVISORY_ACTIONS

    def to_family_stance(self) -> FamilyStance:
        """Map a config-promotion action onto FamilyStance. Raises otherwise."""
        try:
            return _FAMILY_STANCE_BY_ACTION[self]
        except KeyError:
            raise ValueError(
                f"{self} is not a config-promotion action; FamilyStance is "
                "defined only for STRATEGY_FAMILY proposals"
            ) from None


CONFIG_PROMOTION_ACTIONS: frozenset[AgentAction] = frozenset(
    {
        AgentAction.PROPOSE_FAMILY_ENABLE,
        AgentAction.PROPOSE_FAMILY_SHADOW,
        AgentAction.PROPOSE_FAMILY_HALT,
    }
)
# C1 / ACTIVE_PLAN: BOUNDED may attach only to this closed set.
BOUNDED_ACTIONS: frozenset[AgentAction] = CONFIG_PROMOTION_ACTIONS
LIVE_PATH_ACTIONS: frozenset[AgentAction] = frozenset(
    {
        AgentAction.VETO_ENTRY,
        AgentAction.REDUCE_SIZE,
        AgentAction.TIGHTEN_STOP,
        AgentAction.PARTIAL_EXIT,
        AgentAction.FULL_EXIT,
        AgentAction.SUBMIT_ORDER,
        AgentAction.PROPOSE_ROLL,
        AgentAction.PROPOSE_HEDGE,
        AgentAction.PROPOSE_SIZE_INCREASE,
        AgentAction.PROPOSE_ADD,
    }
)
ADVISORY_ACTIONS: frozenset[AgentAction] = frozenset(
    {
        AgentAction.ABSTAIN,
        AgentAction.HOLD,
        AgentAction.RANK_STRUCTURES,
        AgentAction.SELECT_STRIKE_CANDIDATE,
        AgentAction.REQUEST_TERMINAL_POLICY,
        AgentAction.REQUEST_OPERATOR_ATTENTION,
        AgentAction.RECORD_IMPROVEMENT,
    }
)
_FAMILY_STANCE_BY_ACTION: dict[AgentAction, FamilyStance] = {
    AgentAction.PROPOSE_FAMILY_ENABLE: FamilyStance.ENABLE,
    AgentAction.PROPOSE_FAMILY_SHADOW: FamilyStance.SHADOW,
    AgentAction.PROPOSE_FAMILY_HALT: FamilyStance.HALT,
}


@unique
class GateOutcome(StrEnum):
    """What the deterministic gate did with a logged desk output."""

    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    SHADOW_ONLY = "SHADOW_ONLY"


@unique
class Recommendation(StrEnum):
    """Bounded AI output. ABSTAIN is a first-class answer, never an error."""

    ABSTAIN = "ABSTAIN"
    NO_TRADE = "NO_TRADE"
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    LOW_VOLATILITY = "LOW_VOLATILITY"
    REVISE = "REVISE"


@unique
class ReasonCode(StrEnum):
    """Machine-readable cause for a decision, rejection or repair.

    Consumed by alert routing and promotion gates, so it is closed. Prose
    belongs in a separate explanatory field.
    """

    # Accepted
    OK = "OK"

    # Data and freshness
    DATA_STALE = "DATA_STALE"
    DATA_INVALID = "DATA_INVALID"
    DATA_DEGRADED = "DATA_DEGRADED"
    DATA_GAP = "DATA_GAP"
    PRICE_UNAVAILABLE = "PRICE_UNAVAILABLE"
    WARMUP_INCOMPLETE = "WARMUP_INCOMPLETE"
    DIRECTION_FALLBACK = "DIRECTION_FALLBACK"
    DIRECTION_UNRESOLVED = "DIRECTION_UNRESOLVED"
    DIRECTION_NEUTRAL = "DIRECTION_NEUTRAL"
    OPTION_TYPE_MISMATCH = "OPTION_TYPE_MISMATCH"
    MICROSTRUCTURE_UNCONFIRMED = "MICROSTRUCTURE_UNCONFIRMED"
    CONVICTION_BELOW_THRESHOLD = "CONVICTION_BELOW_THRESHOLD"
    REGIME_NOT_RANGE = "REGIME_NOT_RANGE"
    VOL_COMPRESSED = "VOL_COMPRESSED"
    M2_DELTA_FALLBACK = "M2_DELTA_FALLBACK"
    CLOCK_DRIFT = "CLOCK_DRIFT"
    SNAPSHOT_MISMATCH = "SNAPSHOT_MISMATCH"

    # Contract and session
    INSTRUMENT_UNKNOWN = "INSTRUMENT_UNKNOWN"
    CONTRACT_EXPIRED = "CONTRACT_EXPIRED"
    OUTSIDE_SESSION = "OUTSIDE_SESSION"
    EVENT_BLACKOUT = "EVENT_BLACKOUT"
    NON_NIFTY_EXECUTION_REJECTED = "NON_NIFTY_EXECUTION_REJECTED"
    MODE_FAMILY_NOT_PERMITTED = "MODE_FAMILY_NOT_PERMITTED"
    EXPIRY_0_1_DTE_EXCLUDED = "EXPIRY_0_1_DTE_EXCLUDED"
    CALENDAR_NO_ELIGIBLE_EXPIRY = "CALENDAR_NO_ELIGIBLE_EXPIRY"
    INSTRUMENT_MASTER_ABSENT = "INSTRUMENT_MASTER_ABSENT"

    # Risk and capital
    RISK_LIMIT_TRADE = "RISK_LIMIT_TRADE"
    RISK_LIMIT_STRATEGY = "RISK_LIMIT_STRATEGY"
    RISK_LIMIT_PORTFOLIO = "RISK_LIMIT_PORTFOLIO"
    RISK_LIMIT_DAILY_LOSS = "RISK_LIMIT_DAILY_LOSS"
    CAMPAIGN_LOSS_LIMIT = "CAMPAIGN_LOSS_LIMIT"
    CONCENTRATION_LIMIT = "CONCENTRATION_LIMIT"
    CORRELATION_LIMIT = "CORRELATION_LIMIT"
    MARGIN_INSUFFICIENT = "MARGIN_INSUFFICIENT"
    CAPITAL_UNAVAILABLE = "CAPITAL_UNAVAILABLE"
    MAX_LOSS_UNDEFINED = "MAX_LOSS_UNDEFINED"
    SIZE_BELOW_MINIMUM = "SIZE_BELOW_MINIMUM"
    MIN_LOT_EXCEEDS_BUDGET = "MIN_LOT_EXCEEDS_BUDGET"
    ONE_LOT_OVER_GUIDE = "ONE_LOT_OVER_GUIDE"

    # Liquidity and execution
    SPREAD_TOO_WIDE = "SPREAD_TOO_WIDE"
    DEPTH_INSUFFICIENT = "DEPTH_INSUFFICIENT"
    SLIPPAGE_EXCEEDED = "SLIPPAGE_EXCEEDED"
    ORDER_TIMEOUT = "ORDER_TIMEOUT"
    BROKER_REJECTED = "BROKER_REJECTED"
    RATE_LIMIT = "RATE_LIMIT"
    DUPLICATE_IDEMPOTENCY_KEY = "DUPLICATE_IDEMPOTENCY_KEY"
    EXACT_DUPLICATE_SUPPRESSED = "EXACT_DUPLICATE_SUPPRESSED"
    ECONOMIC_OVERLAP_SUPPRESSED = "ECONOMIC_OVERLAP_SUPPRESSED"
    OPPOSING_EXPOSURE_REJECTED = "OPPOSING_EXPOSURE_REJECTED"
    M4_POSITION_CAP_REACHED = "M4_POSITION_CAP_REACHED"
    DAILY_ENTRY_CAP = "DAILY_ENTRY_CAP"
    OPEN_POSITION_CAP = "OPEN_POSITION_CAP"
    PARTIAL_FILL_UNREPAIRED = "PARTIAL_FILL_UNREPAIRED"

    # Operational
    SYSTEM_NOT_READY = "SYSTEM_NOT_READY"
    ENTRY_FROZEN = "ENTRY_FROZEN"
    STRATEGY_HALTED = "STRATEGY_HALTED"
    KILL_SWITCH_ACTIVE = "KILL_SWITCH_ACTIVE"
    BROKER_DISCONNECTED = "BROKER_DISCONNECTED"
    STORAGE_UNCERTAIN = "STORAGE_UNCERTAIN"
    RECONCILIATION_UNRESOLVED = "RECONCILIATION_UNRESOLVED"
    PROTECTIVE_COVERAGE_MISSING = "PROTECTIVE_COVERAGE_MISSING"
    PROTECTION_DEGRADED = "PROTECTION_DEGRADED"
    UNPROTECTED_POSITION = "UNPROTECTED_POSITION"
    UNRECONCILED_POSITION = "UNRECONCILED_POSITION"
    UNKNOWN_ORDER_STATUS = "UNKNOWN_ORDER_STATUS"

    # Contract and configuration integrity
    SCHEMA_VERSION_UNSUPPORTED = "SCHEMA_VERSION_UNSUPPORTED"
    CONFIG_UNVERIFIED = "CONFIG_UNVERIFIED"
    DECISION_EXPIRED = "DECISION_EXPIRED"
    SETUP_COOLDOWN = "SETUP_COOLDOWN"
    ILLEGAL_STATE_TRANSITION = "ILLEGAL_STATE_TRANSITION"

    # Positional review
    REVIEW_DUPLICATE_SLOT = "REVIEW_DUPLICATE_SLOT"
    REVIEW_PROPOSAL_REQUIRES_L2 = "REVIEW_PROPOSAL_REQUIRES_L2"
    PROPOSED_NOT_EXECUTED = "PROPOSED_NOT_EXECUTED"
    STOP_WIDEN_REJECTED = "STOP_WIDEN_REJECTED"
    CALENDAR_SAME_EXPIRY_FORMULA_REFUSED = "CALENDAR_SAME_EXPIRY_FORMULA_REFUSED"
    CALENDAR_EXPERIMENTAL_OFF_STRICT_BOOK = "CALENDAR_EXPERIMENTAL_OFF_STRICT_BOOK"

    # Mode 2 carry gate (P9)
    CARRY_MODE_MISMATCH = "CARRY_MODE_MISMATCH"
    CARRY_THESIS_STALE = "CARRY_THESIS_STALE"
    CARRY_THESIS_INVALID = "CARRY_THESIS_INVALID"
    CARRY_INSUFFICIENT_DTE = "CARRY_INSUFFICIENT_DTE"
    CARRY_OVERNIGHT_BUDGET = "CARRY_OVERNIGHT_BUDGET"
    CARRY_EVENT_BLACKOUT = "CARRY_EVENT_BLACKOUT"
    CARRY_PORTFOLIO_BLOCKED = "CARRY_PORTFOLIO_BLOCKED"
    CARRY_EXIT_STATE_MISSING = "CARRY_EXIT_STATE_MISSING"
    CARRY_RECOVERY_UNHEALTHY = "CARRY_RECOVERY_UNHEALTHY"

    # AI, all of which fall back to a deterministic baseline
    AI_UNAVAILABLE = "AI_UNAVAILABLE"
    AI_SCHEMA_INVALID = "AI_SCHEMA_INVALID"
    AI_PROPOSAL_EXPIRED = "AI_PROPOSAL_EXPIRED"
    AI_EVIDENCE_INSUFFICIENT = "AI_EVIDENCE_INSUFFICIENT"
    AI_ABSTAINED = "AI_ABSTAINED"
    AI_NOT_PROMOTED = "AI_NOT_PROMOTED"
    AI_BUDGET_EXHAUSTED = "AI_BUDGET_EXHAUSTED"
    OPERATOR_ATTENTION = "OPERATOR_ATTENTION"


@unique
class DirectionalClaim(StrEnum):
    """Closed directional stance on the thesis. No free text."""

    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"
    VOL_LONG = "VOL_LONG"
    VOL_SHORT = "VOL_SHORT"


@unique
class DriverCode(StrEnum):
    """Primary thesis driver. Closed vocabulary for SAME_THESIS_DRIVER later."""

    TREND_CONTINUATION = "TREND_CONTINUATION"
    MEAN_REVERSION = "MEAN_REVERSION"
    VOL_EXPANSION = "VOL_EXPANSION"
    VOL_COMPRESSION = "VOL_COMPRESSION"
    EVENT_DRIVEN = "EVENT_DRIVEN"
    FLOW_IMBALANCE = "FLOW_IMBALANCE"
    STRUCTURE_SKEW = "STRUCTURE_SKEW"
    OTHER = "OTHER"


@unique
class InvalidationMetric(StrEnum):
    """Machine-evaluable thesis invalidation metrics. No prose metrics."""

    SPOT_PCT_FROM_ENTRY = "SPOT_PCT_FROM_ENTRY"
    IV_PERCENTILE = "IV_PERCENTILE"
    TREND_SCORE = "TREND_SCORE"
    OI_CHANGE_PCT = "OI_CHANGE_PCT"
    ATR_MULTIPLE = "ATR_MULTIPLE"
    DTE = "DTE"
    MAE_R = "MAE_R"
    DELTA = "DELTA"
    VEGA_PNL_R = "VEGA_PNL_R"
    EVENT_RISK_STATE = "EVENT_RISK_STATE"
    REALIZED_VOL_RATIO = "REALIZED_VOL_RATIO"
    INDIA_VIX = "INDIA_VIX"


@unique
class Comparator(StrEnum):
    """How an observed metric is compared to an InvalidationCondition threshold."""

    LT = "LT"
    LTE = "LTE"
    GT = "GT"
    GTE = "GTE"
    EQ = "EQ"
    CROSSES_BELOW = "CROSSES_BELOW"
    CROSSES_ABOVE = "CROSSES_ABOVE"


@unique
class InvalidationSeverity(StrEnum):
    """SOFT = tighten / warn; HARD = thesis broken, exit path."""

    SOFT = "SOFT"
    HARD = "HARD"


@unique
class InvalidationStatus(StrEnum):
    """Result of evaluating one InvalidationCondition against observations."""

    HOLDING = "HOLDING"
    TRIGGERED = "TRIGGERED"
    MISSING_OBSERVATION = "MISSING_OBSERVATION"


@unique
class ImprovementArea(StrEnum):
    """Closed taxonomy for ImprovementRecord.area (PART 9.2)."""

    ENTRY_TIMING = "ENTRY_TIMING"
    STRIKE_SELECTION = "STRIKE_SELECTION"
    SIZING = "SIZING"
    EXIT_RULE = "EXIT_RULE"
    CORRELATION = "CORRELATION"
    DATA_GAP = "DATA_GAP"
    NEWS_COVERAGE = "NEWS_COVERAGE"
    EXECUTION = "EXECUTION"
    COST = "COST"
    RISK_LIMIT = "RISK_LIMIT"
    THESIS_QUALITY = "THESIS_QUALITY"
    TOOLING = "TOOLING"
    PROCESS = "PROCESS"


@unique
class TestabilityKind(StrEnum):
    """How an improvement claim can be verified."""

    BACKTEST = "BACKTEST"
    SHADOW_RULE = "SHADOW_RULE"
    CONFIG_CHANGE = "CONFIG_CHANGE"
    CODE_CHANGE = "CODE_CHANGE"
    DATA_ACQUISITION = "DATA_ACQUISITION"
    NOT_TESTABLE = "NOT_TESTABLE"


@unique
class ImprovementStatus(StrEnum):
    """Lifecycle of an ImprovementRecord."""

    OPEN = "OPEN"
    CLUSTERED = "CLUSTERED"
    PROMOTED_TO_HYPOTHESIS = "PROMOTED_TO_HYPOTHESIS"
    IMPLEMENTED = "IMPLEMENTED"
    REJECTED = "REJECTED"
    STALE = "STALE"


@unique
class AgentReasonCode(StrEnum):
    """Closed vocabulary of reason codes an agent may cite (PART 11.1)."""

    VOLATILITY_EXPANSION = "VOLATILITY_EXPANSION"
    VOLATILITY_COMPRESSION = "VOLATILITY_COMPRESSION"
    REGIME_ALIGNMENT = "REGIME_ALIGNMENT"
    TREND_CONFIRMATION = "TREND_CONFIRMATION"
    MEAN_REVERSION_SETUP = "MEAN_REVERSION_SETUP"
    LIQUIDITY_ADEQUATE = "LIQUIDITY_ADEQUATE"
    EVENT_CLEAR = "EVENT_CLEAR"
    STRUCTURE_DEFINED_RISK = "STRUCTURE_DEFINED_RISK"


@unique
class LiquidityGrade(StrEnum):
    """Deterministic liquidity floor for strike shortlist candidates."""

    A = "A"
    B = "B"
    C = "C"


@unique
class VetoCode(StrEnum):
    """Closed ENTRY veto vocabulary. Required when action is VETO_ENTRY."""

    THESIS_WEAK = "THESIS_WEAK"
    LIQUIDITY_MARGINAL = "LIQUIDITY_MARGINAL"
    EVENT_RISK = "EVENT_RISK"
    CORRELATION_OVERLAP = "CORRELATION_OVERLAP"
    SIZE_TOO_LARGE = "SIZE_TOO_LARGE"
    SPREAD_WIDE = "SPREAD_WIDE"
    DEPTH_GAP = "DEPTH_GAP"
    OTHER_GROUNDED = "OTHER_GROUNDED"


@unique
class EntryGateId(StrEnum):
    """Closed gate ids ENTRY advice may cite in failed_gate_ids."""

    SHORTLIST_TOO_SMALL = "SHORTLIST_TOO_SMALL"
    CANDIDATE_NOT_ON_SHORTLIST = "CANDIDATE_NOT_ON_SHORTLIST"
    SIZE_MULTIPLIER_INVALID = "SIZE_MULTIPLIER_INVALID"
    THESIS_REQUIRED = "THESIS_REQUIRED"
    VETO_CODES_REQUIRED = "VETO_CODES_REQUIRED"
    ACTION_NOT_ALLOWED = "ACTION_NOT_ALLOWED"


@unique
class SharedFateCode(StrEnum):
    """Closed PORTFOLIO shared-fate taxonomy (PART 6.2)."""

    SAME_SCHEDULED_EVENT = "SAME_SCHEDULED_EVENT"
    SAME_POLICY_DIRECTION = "SAME_POLICY_DIRECTION"
    SAME_VOLATILITY_DIRECTION = "SAME_VOLATILITY_DIRECTION"
    SAME_LIQUIDITY_REGIME = "SAME_LIQUIDITY_REGIME"
    SAME_EXPIRY_PIN = "SAME_EXPIRY_PIN"
    SAME_GLOBAL_FACTOR = "SAME_GLOBAL_FACTOR"
    SAME_THESIS_DRIVER = "SAME_THESIS_DRIVER"
    NO_SHARED_FATE = "NO_SHARED_FATE"


@unique
class ThesisVerdict(StrEnum):
    """Four-cell POSTTRADE thesis outcome (PART 9.1)."""

    CORRECT_AND_PAID = "CORRECT_AND_PAID"
    CORRECT_UNPAID = "CORRECT_UNPAID"
    WRONG_AND_LOST = "WRONG_AND_LOST"
    WRONG_BUT_PAID = "WRONG_BUT_PAID"
    UNTESTED = "UNTESTED"


@unique
class AttributionCode(StrEnum):
    """Primary POSTTRADE attribution (PART 9.1)."""

    DIRECTION = "DIRECTION"
    VOLATILITY = "VOLATILITY"
    THETA = "THETA"
    EXECUTION_SLIPPAGE = "EXECUTION_SLIPPAGE"
    CHARGES = "CHARGES"
    SIZING = "SIZING"
    TIMING = "TIMING"
    EXIT_RULE = "EXIT_RULE"
    LUCK = "LUCK"


@unique
class EventClass(StrEnum):
    """Closed MACRO event taxonomy (PART 10.2)."""

    RBI_POLICY = "RBI_POLICY"
    FED = "FED"
    CPI = "CPI"
    GDP = "GDP"
    BUDGET = "BUDGET"
    EARNINGS = "EARNINGS"
    GEOPOLITICAL = "GEOPOLITICAL"
    REGULATORY_SEBI = "REGULATORY_SEBI"
    EXPIRY_MECHANICS = "EXPIRY_MECHANICS"
    GLOBAL_RISK_OFF = "GLOBAL_RISK_OFF"
    OTHER = "OTHER"


@unique
class MacroEventSeverity(StrEnum):
    """Scheduled-event uncertainty for MacroCalendar gates."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


@unique
class TerminalPolicyKind(StrEnum):
    """Closed terminal-policy vocabulary (AGENT_DESK_SPEC PART 5.1)."""

    FLATTEN_AT_DTE = "FLATTEN_AT_DTE"
    RUN_TO_EXPIRY_DEFINED_RISK = "RUN_TO_EXPIRY_DEFINED_RISK"
    FLATTEN_EARLY_IF_FRAGILE = "FLATTEN_EARLY_IF_FRAGILE"


@unique
class TerminalPolicyRejectReason(StrEnum):
    """Closed reasons the PART 5.2 eligibility gate may refuse run-to-expiry."""

    STRUCTURE_NOT_DEFINED_RISK = "STRUCTURE_NOT_DEFINED_RISK"
    ASSIGNMENT_RISK_AT_EXPIRY = "ASSIGNMENT_RISK_AT_EXPIRY"
    NOT_CASH_SETTLED = "NOT_CASH_SETTLED"
    MAX_LOSS_MISMATCH = "MAX_LOSS_MISMATCH"
    EXTRINSIC_ABOVE_FLOOR = "EXTRINSIC_ABOVE_FLOOR"
    TERMINAL_EXPOSURE_CAP = "TERMINAL_EXPOSURE_CAP"
    LIQUIDITY_BELOW_A = "LIQUIDITY_BELOW_A"
    EVENT_RISK_NOT_NORMAL = "EVENT_RISK_NOT_NORMAL"


@unique
class PlaybookTriggerKind(StrEnum):
    """Detectable regime-break / fragility triggers for TailPlaybook edits."""

    GAP_BEYOND_ATR = "GAP_BEYOND_ATR"
    VIX_JUMP = "VIX_JUMP"
    SPREAD_WIDENING = "SPREAD_WIDENING"
    FEED_QUALITY_DEGRADED = "FEED_QUALITY_DEGRADED"
    TAIL_BUDGET_BREACH = "TAIL_BUDGET_BREACH"
    BIAS_BAND_BREACH = "BIAS_BAND_BREACH"


@unique
class PlaybookEditKind(StrEnum):
    """Structured playbook-edit actions. Human signs; never auto-implemented."""

    ADD_RESPONSE = "ADD_RESPONSE"
    TIGHTEN_THRESHOLD = "TIGHTEN_THRESHOLD"
    DISABLE_RESPONSE = "DISABLE_RESPONSE"
    REVIEW_ONLY = "REVIEW_ONLY"


@unique
class DemotionReason(StrEnum):
    """Closed reasons a desk may be demoted (PART 14.2)."""

    HALLUCINATION_RATE = "HALLUCINATION_RATE"
    SCHEMA_FAILURE_RATE = "SCHEMA_FAILURE_RATE"
    BRIER_DEGRADATION = "BRIER_DEGRADATION"
    SAFETY_INVARIANT = "SAFETY_INVARIANT"
    GRANT_EXPIRED = "GRANT_EXPIRED"
    VERSION_TRIPLE_MISMATCH = "VERSION_TRIPLE_MISMATCH"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    MANUAL = "MANUAL"
