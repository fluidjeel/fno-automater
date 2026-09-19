"""Closed enumerations shared by every contract and state machine.

Every set here is deliberately closed. A free-text status or reason would leak
into dashboards, alert routing and promotion gates, where a typo becomes an
unmonitored failure mode. Adding a member is a contract change.
"""

from __future__ import annotations

from enum import StrEnum, unique

__all__ = [
    "AssetClass",
    "AttentionBlocker",
    "DataQuality",
    "DifferenceClass",
    "EligibilityStatus",
    "Exchange",
    "ExecutionMode",
    "ExitScope",
    "FamilyStance",
    "FillOutcome",
    "HoldingStyle",
    "InstrumentKind",
    "IntentState",
    "OptionType",
    "OrderPlanState",
    "OrderState",
    "OrderType",
    "ProposalType",
    "ReadinessLevel",
    "ReasonCode",
    "Recommendation",
    "ReconciliationTrigger",
    "ReservationState",
    "ReviewAction",
    "ReviewSlotId",
    "RiskAction",
    "Severity",
    "Side",
    "SizingBindingConstraint",
    "SystemState",
    "TimeInForce",
    "TradeState",
    "Trigger",
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

    @property
    def is_proposal(self) -> bool:
        """HEDGE/ROLL are new trades; they never auto-submit."""
        return self in {ReviewAction.PROPOSE_HEDGE, ReviewAction.PROPOSE_ROLL}

    @property
    def submits_exit(self) -> bool:
        return self in {ReviewAction.PARTIAL_EXIT, ReviewAction.FULL_EXIT}


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
class FamilyStance(StrEnum):
    """Proposed paper/shadow posture for one strategy family. Never a live switch."""

    ENABLE = "ENABLE"
    SHADOW = "SHADOW"
    HALT = "HALT"


@unique
class AttentionBlocker(StrEnum):
    """Why the advisory loop cannot proceed without an operator artifact."""

    CHARGES_UNVERIFIED = "CHARGES_UNVERIFIED"
    CAS_FEATURES_MISSING = "CAS_FEATURES_MISSING"
    LIVE_CONFIG_UNVERIFIED = "LIVE_CONFIG_UNVERIFIED"
    DATA_GAP = "DATA_GAP"
    AGENT_DISABLED = "AGENT_DISABLED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"


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
    CLOCK_DRIFT = "CLOCK_DRIFT"
    SNAPSHOT_MISMATCH = "SNAPSHOT_MISMATCH"

    # Contract and session
    INSTRUMENT_UNKNOWN = "INSTRUMENT_UNKNOWN"
    CONTRACT_EXPIRED = "CONTRACT_EXPIRED"
    OUTSIDE_SESSION = "OUTSIDE_SESSION"
    EVENT_BLACKOUT = "EVENT_BLACKOUT"

    # Risk and capital
    RISK_LIMIT_TRADE = "RISK_LIMIT_TRADE"
    RISK_LIMIT_STRATEGY = "RISK_LIMIT_STRATEGY"
    RISK_LIMIT_PORTFOLIO = "RISK_LIMIT_PORTFOLIO"
    RISK_LIMIT_DAILY_LOSS = "RISK_LIMIT_DAILY_LOSS"
    CONCENTRATION_LIMIT = "CONCENTRATION_LIMIT"
    CORRELATION_LIMIT = "CORRELATION_LIMIT"
    MARGIN_INSUFFICIENT = "MARGIN_INSUFFICIENT"
    CAPITAL_UNAVAILABLE = "CAPITAL_UNAVAILABLE"
    MAX_LOSS_UNDEFINED = "MAX_LOSS_UNDEFINED"
    SIZE_BELOW_MINIMUM = "SIZE_BELOW_MINIMUM"

    # Liquidity and execution
    SPREAD_TOO_WIDE = "SPREAD_TOO_WIDE"
    DEPTH_INSUFFICIENT = "DEPTH_INSUFFICIENT"
    SLIPPAGE_EXCEEDED = "SLIPPAGE_EXCEEDED"
    ORDER_TIMEOUT = "ORDER_TIMEOUT"
    BROKER_REJECTED = "BROKER_REJECTED"
    RATE_LIMIT = "RATE_LIMIT"
    DUPLICATE_IDEMPOTENCY_KEY = "DUPLICATE_IDEMPOTENCY_KEY"
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
    STOP_WIDEN_REJECTED = "STOP_WIDEN_REJECTED"

    # AI, all of which fall back to a deterministic baseline
    AI_UNAVAILABLE = "AI_UNAVAILABLE"
    AI_SCHEMA_INVALID = "AI_SCHEMA_INVALID"
    AI_PROPOSAL_EXPIRED = "AI_PROPOSAL_EXPIRED"
    AI_EVIDENCE_INSUFFICIENT = "AI_EVIDENCE_INSUFFICIENT"
    AI_ABSTAINED = "AI_ABSTAINED"
    AI_NOT_PROMOTED = "AI_NOT_PROMOTED"
    AI_BUDGET_EXHAUSTED = "AI_BUDGET_EXHAUSTED"
    OPERATOR_ATTENTION = "OPERATOR_ATTENTION"
