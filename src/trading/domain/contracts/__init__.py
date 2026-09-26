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
from trading.domain.contracts.base import (
    SCHEMA_VERSION,
    ContractError,
    StrictModel,
    VersionedModel,
)
from trading.domain.contracts.bias import (
    BiasMetricId,
    BiasMetricResult,
    BiasReport,
)
from trading.domain.contracts.carry import (
    CarryGateDecision,
    M2CarryGateConfig,
    M2CarryGateInput,
    PositionCarryRecord,
)
from trading.domain.contracts.common import (
    ContractRef,
    DataQualityReport,
    ExposureSnapshot,
    Lineage,
    Versions,
)
from trading.domain.contracts.confidence_sizing import (
    PHASE1_M_CEILING,
    PHASE1_M_FLOOR,
    PHASE1_SIZE_MULTIPLIERS,
    Phase1SizingAdvice,
    bucket_from_confidence,
    phase1_size_multiplier,
)
from trading.domain.contracts.cycle_evidence import (
    PaperCycleEvidence,
    StrategyCycleSummary,
)
from trading.domain.contracts.discovery_decision import DiscoveryDecision
from trading.domain.contracts.entry import (
    ALLOWED_ENTRY_ACTIONS,
    EntryAdvice,
    EntryAdviceError,
    LegSpec,
    StrikeCandidate,
    StrikeShortlist,
    assert_candidate_on_shortlist,
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
from trading.domain.contracts.exposure import (
    CorrelationPair,
    EventOverlap,
    ExposureReport,
    NotionalBucket,
)
from trading.domain.contracts.hallucination import (
    GroundingResult,
    HallucinationEvent,
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
from trading.domain.contracts.improvement import (
    ImprovementCluster,
    ImprovementRecord,
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
    RollSwitchTransition,
)
from trading.domain.contracts.mode_policy import (
    ModePolicy,
    ModesConfig,
    load_modes_config,
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
from trading.domain.contracts.phase2_sizing import (
    PHASE2_HARD_CAP,
    PHASE2_M_CEILING_DEFAULT,
    CalibrationGate,
    Phase2UpscaleEnvelope,
    agent_size_multiplier_rejected_above_one,
    apply_deterministic_upscale,
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
from trading.domain.contracts.stress import (
    FragilityFlag,
    ScenarioResult,
    StressReport,
    StressScenario,
    StressScenarioId,
)
from trading.domain.contracts.terminal_policy import (
    DEFAULT_FLATTEN_DTE,
    TerminalPolicy,
    TerminalPolicyDecision,
    TerminalPolicyEligibilityInput,
    TerminalPolicyRequest,
)
from trading.domain.contracts.trade_thesis import (
    InvalidationCondition,
    TradeThesis,
)
from trading.domain.enums import FamilyId, ModeId

__all__ = [
    "ALLOWED_ENTRY_ACTIONS",
    "DEFAULT_FLATTEN_DTE",
    "PHASE1_M_CEILING",
    "PHASE1_M_FLOOR",
    "PHASE1_SIZE_MULTIPLIERS",
    "PHASE2_HARD_CAP",
    "PHASE2_M_CEILING_DEFAULT",
    "SCHEMA_VERSION",
    "AIProposal",
    "AdviceStance",
    "AgentDecision",
    "ApprovedLeg",
    "AttentionRequest",
    "AuthorityGrant",
    "BiasMetricId",
    "BiasMetricResult",
    "BiasReport",
    "CalibrationGate",
    "CandidateBinding",
    "CapitalReservation",
    "CarryGateDecision",
    "CohortPackage",
    "CohortScorecard",
    "CohortSignal",
    "ConfidenceKind",
    "ContractError",
    "ContractRef",
    "CorrelationPair",
    "DataQualityReport",
    "DerivativesContext",
    "DiscoveryCandidateRow",
    "DiscoveryDecision",
    "DiscoveryDecisionInputs",
    "DiscoveryFillSnapshot",
    "DiscoverySizingSnapshot",
    "EntryAdvice",
    "EntryAdviceError",
    "EntryFreezeRecord",
    "EntryPolicy",
    "EventOverlap",
    "EvidenceRef",
    "ExitPolicy",
    "ExitTemplate",
    "ExperimentDefinition",
    "ExposureReport",
    "ExposureSnapshot",
    "FamilyAction",
    "FamilyId",
    "FeatureSnapshot",
    "FillSimulation",
    "FragilityFlag",
    "Greeks",
    "GroundingResult",
    "HallucinationEvent",
    "ImprovementCluster",
    "ImprovementRecord",
    "InstrumentSpec",
    "IntentConstraints",
    "IntentLeg",
    "InvalidationCondition",
    "JudgmentReport",
    "JudgmentSignalResult",
    "LegQuoteRef",
    "LegSpec",
    "Lineage",
    "M2CarryGateConfig",
    "M2CarryGateInput",
    "MacroStatus",
    "MarketQuote",
    "MarketState",
    "ModeId",
    "ModePolicy",
    "ModelVersions",
    "ModesConfig",
    "NotionalBucket",
    "OrderCommand",
    "OrderEvent",
    "OrderIdentity",
    "OrderPlan",
    "PaperCycleEvidence",
    "PaperDataAssessment",
    "PaperDataField",
    "PaperDataFieldResult",
    "PaperDataFieldSpec",
    "PaperDataPresence",
    "PaperDataRequirements",
    "PaperDataTier",
    "ParameterProposal",
    "PendingOrderSummary",
    "Phase1SizingAdvice",
    "Phase2UpscaleEnvelope",
    "PlannedOrder",
    "PortfolioSnapshot",
    "PortfolioView",
    "PositionCarryRecord",
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
    "RollSwitchTransition",
    "RouteDecision",
    "ScenarioResult",
    "SessionProtectionState",
    "SetupFeatures",
    "SizingDecision",
    "SizingLegResult",
    "SizingLimits",
    "SizingRequest",
    "SnapshotTimes",
    "StrategyCycleSummary",
    "StressReport",
    "StressScenario",
    "StressScenarioId",
    "StrictModel",
    "StrikeCandidate",
    "StrikeShortlist",
    "StructureAdvice",
    "StructureChoice",
    "StructureKind",
    "TerminalPolicy",
    "TerminalPolicyDecision",
    "TerminalPolicyEligibilityInput",
    "TerminalPolicyRequest",
    "TradeIntent",
    "TradeThesis",
    "TrendState",
    "UnderlyingExposure",
    "VersionedModel",
    "Versions",
    "VolatilityState",
    "agent_size_multiplier_rejected_above_one",
    "apply_deterministic_upscale",
    "assert_candidate_on_shortlist",
    "bucket_from_confidence",
    "load_modes_config",
    "phase1_size_multiplier",
]
