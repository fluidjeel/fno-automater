"""Typed, versioned contracts. Semantics and authority are fixed here."""

from trading.domain.contracts.advice import (
    AdviceStance,
    RankedStructure,
    StructureAdvice,
    StructureChoice,
)
from trading.domain.contracts.agent_decision import AgentDecision
from trading.domain.contracts.attention import AttentionRequest
from trading.domain.contracts.authority import AuthorityGrant
from trading.domain.contracts.trade_thesis import (
    InvalidationCondition,
    TradeThesis,
)
from trading.domain.contracts.base import (
    SCHEMA_VERSION,
    ContractError,
    StrictModel,
    VersionedModel,
)
from trading.domain.contracts.common import (
    ContractRef,
    DataQualityReport,
    ExposureSnapshot,
    Lineage,
    Versions,
)
from trading.domain.contracts.entry_freeze import EntryFreezeRecord
from trading.domain.contracts.evaluation import (
    CohortPackage,
    CohortScorecard,
    CohortSignal,
    ExperimentDefinition,
    FillSimulation,
    JudgmentReport,
    JudgmentSignalResult,
    PromotionEligibilityResult,
    ReasonCount,
)
from trading.domain.contracts.identification import (
    CandidateBinding,
    ConfidenceKind,
    MacroStatus,
    MarketState,
    RouteDecision,
    SetupFeatures,
    StructureKind,
    TrendState,
    VolatilityState,
)
from trading.domain.contracts.instrument import InstrumentSpec
from trading.domain.contracts.intent import (
    EntryPolicy,
    ExitTemplate,
    IntentConstraints,
    IntentLeg,
    TradeIntent,
)
from trading.domain.contracts.lifecycle import (
    PositionLifecycleRecord,
    PositionReviewRecord,
)
from trading.domain.contracts.order import OrderCommand, OrderEvent, OrderIdentity
from trading.domain.contracts.order_plan import (
    OrderPlan,
    PlannedOrder,
    ProtectiveOrderStub,
)
from trading.domain.contracts.paper_data import (
    PaperDataAssessment,
    PaperDataField,
    PaperDataFieldResult,
    PaperDataFieldSpec,
    PaperDataPresence,
    PaperDataRequirements,
    PaperDataTier,
)
from trading.domain.contracts.portfolio import (
    PendingOrderSummary,
    PortfolioSnapshot,
    PortfolioView,
    PositionRecord,
    UnderlyingExposure,
)
from trading.domain.contracts.position import (
    ExitPolicy,
    PositionLegState,
    PositionState,
)
from trading.domain.contracts.proposal import (
    AIProposal,
    EvidenceRef,
    FamilyAction,
    ModelVersions,
    ParameterProposal,
)
from trading.domain.contracts.protection import (
    ProtectionHeartbeat,
    ProtectionObservation,
    ProtectionStateRecord,
    SessionProtectionState,
)
from trading.domain.contracts.reconciliation import ReconciliationEvent
from trading.domain.contracts.reconciliation_result import ReconciliationResult
from trading.domain.contracts.reservation import CapitalReservation
from trading.domain.contracts.risk import ApprovedLeg, LegQuoteRef, RiskDecision
from trading.domain.contracts.sizing import (
    SizingDecision,
    SizingLegResult,
    SizingLimits,
    SizingRequest,
)
from trading.domain.contracts.snapshot import (
    DerivativesContext,
    FeatureSnapshot,
    Greeks,
    MarketQuote,
    SnapshotTimes,
)

__all__ = [
    "SCHEMA_VERSION",
    "AIProposal",
    "AdviceStance",
    "AgentDecision",
    "ApprovedLeg",
    "AttentionRequest",
    "AuthorityGrant",
    "InvalidationCondition",
    "TradeThesis",
    "CandidateBinding",
    "CapitalReservation",
    "CohortPackage",
    "CohortScorecard",
    "CohortSignal",
    "ConfidenceKind",
    "ContractError",
    "ContractRef",
    "DataQualityReport",
    "DerivativesContext",
    "EntryFreezeRecord",
    "EntryPolicy",
    "EvidenceRef",
    "ExitPolicy",
    "ExitTemplate",
    "ExperimentDefinition",
    "ExposureSnapshot",
    "FamilyAction",
    "FeatureSnapshot",
    "FillSimulation",
    "Greeks",
    "InstrumentSpec",
    "IntentConstraints",
    "IntentLeg",
    "JudgmentReport",
    "JudgmentSignalResult",
    "LegQuoteRef",
    "Lineage",
    "MacroStatus",
    "MarketQuote",
    "MarketState",
    "ModelVersions",
    "OrderCommand",
    "OrderEvent",
    "OrderIdentity",
    "OrderPlan",
    "PaperDataAssessment",
    "PaperDataField",
    "PaperDataFieldResult",
    "PaperDataFieldSpec",
    "PaperDataPresence",
    "PaperDataRequirements",
    "PaperDataTier",
    "ParameterProposal",
    "PendingOrderSummary",
    "PlannedOrder",
    "PortfolioSnapshot",
    "PortfolioView",
    "PositionLegState",
    "PositionLifecycleRecord",
    "PositionRecord",
    "PositionReviewRecord",
    "PositionState",
    "PromotionEligibilityResult",
    "ProtectionHeartbeat",
    "ProtectionObservation",
    "ProtectionStateRecord",
    "ProtectiveOrderStub",
    "RankedStructure",
    "ReasonCount",
    "ReconciliationEvent",
    "ReconciliationResult",
    "RiskDecision",
    "RouteDecision",
    "SessionProtectionState",
    "SetupFeatures",
    "SizingDecision",
    "SizingLegResult",
    "SizingLimits",
    "SizingRequest",
    "SnapshotTimes",
    "StrictModel",
    "StructureAdvice",
    "StructureChoice",
    "StructureKind",
    "TradeIntent",
    "TrendState",
    "UnderlyingExposure",
    "VersionedModel",
    "Versions",
    "VolatilityState",
]
