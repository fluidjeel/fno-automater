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
from trading.domain.contracts.intent import (
    EntryPolicy,
    ExitTemplate,
    IntentConstraints,
    IntentLeg,
    TradeIntent,
)
from trading.domain.contracts.order import OrderCommand, OrderEvent, OrderIdentity
from trading.domain.contracts.proposal import (
    AIProposal,
    EvidenceRef,
    ModelVersions,
    ParameterProposal,
)
from trading.domain.contracts.reconciliation import ReconciliationEvent
from trading.domain.contracts.risk import ApprovedLeg, RiskDecision
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
    "ContractError",
    "ContractRef",
    "DataQualityReport",
    "DerivativesContext",
    "EntryPolicy",
    "EvidenceRef",
    "ExitTemplate",
    "ExposureSnapshot",
    "FeatureSnapshot",
    "Greeks",
    "IntentConstraints",
    "IntentLeg",
    "Lineage",
    "MarketQuote",
    "ModelVersions",
    "OrderCommand",
    "OrderEvent",
    "OrderIdentity",
    "ParameterProposal",
    "ReconciliationEvent",
    "RiskDecision",
    "SnapshotTimes",
    "StrictModel",
    "TradeIntent",
    "VersionedModel",
    "Versions",
]
