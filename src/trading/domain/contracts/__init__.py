"""Typed, versioned contracts. Semantics and authority are fixed here."""

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
from trading.domain.contracts.instrument import InstrumentSpec
from trading.domain.contracts.intent import (
    EntryPolicy,
    ExitTemplate,
    IntentConstraints,
    IntentLeg,
    TradeIntent,
)
from trading.domain.contracts.order import OrderCommand, OrderEvent, OrderIdentity
from trading.domain.contracts.order_plan import (
    OrderPlan,
    PlannedOrder,
    ProtectiveOrderStub,
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
    ModelVersions,
    ParameterProposal,
)
from trading.domain.contracts.reconciliation import ReconciliationEvent
from trading.domain.contracts.reconciliation_result import ReconciliationResult
from trading.domain.contracts.reservation import CapitalReservation
from trading.domain.contracts.risk import ApprovedLeg, RiskDecision
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
    "ApprovedLeg",
    "CapitalReservation",
    "ContractError",
    "ContractRef",
    "DataQualityReport",
    "DerivativesContext",
    "EntryPolicy",
    "EvidenceRef",
    "ExitPolicy",
    "ExitTemplate",
    "ExposureSnapshot",
    "FeatureSnapshot",
    "Greeks",
    "InstrumentSpec",
    "IntentConstraints",
    "IntentLeg",
    "Lineage",
    "MarketQuote",
    "ModelVersions",
    "OrderCommand",
    "OrderEvent",
    "OrderIdentity",
    "OrderPlan",
    "ParameterProposal",
    "PendingOrderSummary",
    "PlannedOrder",
    "PortfolioSnapshot",
    "PortfolioView",
    "PositionLegState",
    "PositionRecord",
    "PositionState",
    "ProtectiveOrderStub",
    "ReconciliationEvent",
    "ReconciliationResult",
    "RiskDecision",
    "SizingDecision",
    "SizingLegResult",
    "SizingLimits",
    "SizingRequest",
    "SnapshotTimes",
    "StrictModel",
    "TradeIntent",
    "UnderlyingExposure",
    "VersionedModel",
    "Versions",
]
