"""TradeIntent to RiskDecision orchestration. Invariant 4."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum, unique
from typing import TypedDict

from trading.broker.ports import MarginPreviewPort
from trading.config.loader import LoadedConfig
from trading.config.risk_policy import LoadedRiskPolicy, RiskPolicyConfig
from trading.config.schema import RiskLimits
from trading.domain.clock import Clock
from trading.domain.contracts.instrument import InstrumentSpec
from trading.domain.contracts.intent import TradeIntent
from trading.domain.contracts.portfolio import PortfolioSnapshot
from trading.domain.contracts.risk import ApprovedLeg, LegQuoteRef, RiskDecision
from trading.domain.contracts.sizing import SizingRequest
from trading.domain.contracts.snapshot import FeatureSnapshot
from trading.domain.enums import (
    DataQuality,
    InstrumentKind,
    ReasonCode,
    ReservationState,
    RiskAction,
    Side,
)
from trading.domain.ids import IdFactory
from trading.domain.primitives import Lots, LotSize, Money, Percent
from trading.news.contracts import EventRiskState, EventRiskStatus, NewsQuality
from trading.risk.limits import (
    build_sizing_limits,
    evaluate_pre_trade_limits,
    project_post_trade_exposure,
)
from trading.risk.reservation import CapitalReservationService
from trading.risk.sizing.commodity_future import (
    CommodityFutureSizingEngine,
    is_commodity_future,
)
from trading.risk.sizing.credit_spread import (
    CreditSpreadSizingEngine,
    credit_spread_legs,
    is_credit_spread,
)
from trading.risk.sizing.debit_spread import (
    DebitSpreadSizingEngine,
    is_debit_spread,
    spread_legs,
)
from trading.risk.sizing.iron_condor import (
    IronCondorLegs,
    IronCondorSizingEngine,
    is_iron_condor,
)
from trading.risk.sizing.long_option import LongOptionSizingEngine, LotBounds
from trading.risk.snapshot_bundle import validate_leg_snapshot_bundle

__all__ = ["RiskGateway", "RiskGatewayRequest"]


class _SizingError(ValueError):
    """Quotes or geometry could not be sized."""


@unique
class _StructureKind(StrEnum):
    LONG_OPTION = "LONG_OPTION"
    DEBIT_SPREAD = "DEBIT_SPREAD"
    CREDIT_SPREAD = "CREDIT_SPREAD"
    IRON_CONDOR = "IRON_CONDOR"
    COMMODITY_FUTURE = "COMMODITY_FUTURE"


@dataclass(frozen=True, slots=True)
class _SizingOutcome:
    approved_lots: int
    bounds: LotBounds
    recalculated_max_loss: Money
    estimated_margin: Money
    approved_legs: tuple[ApprovedLeg, ...]
    net_delta_delta: int


@dataclass(frozen=True, slots=True)
class RiskGatewayRequest:
    """Inputs required to evaluate one intent."""

    intent: TradeIntent
    feature_snapshot: FeatureSnapshot
    portfolio_snapshot: PortfolioSnapshot
    instrument: InstrumentSpec
    leg_snapshots: Mapping[str, FeatureSnapshot] = field(default_factory=dict)
    # None means the caller supplied no reviewed event state. A strategy that
    # requires a clear blackout must then fail closed.
    event_risk_state: EventRiskState | None = None


class _SnapshotAudit(TypedDict):
    decision_snapshot_id: str
    decision_timestamp: datetime
    leg_quotes: tuple[LegQuoteRef, ...]


class RiskGateway:
    """Deterministic pre-trade gate: size, limit-check, reserve, decide."""

    def __init__(
        self,
        *,
        account_config: LoadedConfig,
        risk_policy: LoadedRiskPolicy,
        reservation_service: CapitalReservationService,
        margin_preview: MarginPreviewPort,
        clock: Clock,
        id_factory: IdFactory,
    ) -> None:
        self._account_config = account_config
        self._risk_policy = risk_policy
        self._reservations = reservation_service
        self._margin_preview = margin_preview
        self._clock = clock
        self._ids = id_factory
        self._long_option_sizer = LongOptionSizingEngine()
        self._debit_spread_sizer = DebitSpreadSizingEngine()
        self._credit_spread_sizer = CreditSpreadSizingEngine()
        self._iron_condor_sizer = IronCondorSizingEngine()
        self._commodity_future_sizer = CommodityFutureSizingEngine()

    def evaluate(self, request: RiskGatewayRequest) -> RiskDecision:
        """Return an approval with reserved capital or a machine-readable rejection."""
        now = self._clock.now_utc()
        intent = request.intent
        portfolio = request.portfolio_snapshot
        feature = request.feature_snapshot
        policy = self._risk_policy.config
        account_risk = self._account_config.config.risk
        pre_trade = portfolio.exposure
        audit = _primary_audit(intent, feature, request.leg_snapshots, now)

        if not intent.is_live_at(now):
            return self._reject(
                intent,
                portfolio,
                reason_codes=(ReasonCode.DECISION_EXPIRED,),
                decided_at=now,
                audit=audit,
            )
        if intent.snapshot_id != feature.snapshot_id:
            return self._reject(
                intent,
                portfolio,
                reason_codes=(ReasonCode.SNAPSHOT_MISMATCH,),
                decided_at=now,
                audit=audit,
            )
        if not feature.permits_new_exposure:
            code = _quality_reason(feature.quality.state)
            return self._reject(
                intent,
                portfolio,
                reason_codes=(code,),
                decided_at=now,
                audit=audit,
            )

        constraint_reason = _constraint_reason(request, now=now)
        if constraint_reason is not None:
            return self._reject(
                intent,
                portfolio,
                reason_codes=(constraint_reason,),
                decided_at=now,
                audit=audit,
            )

        structure = _detect_structure(request)
        if structure is None:
            return self._reject(
                intent,
                portfolio,
                reason_codes=(ReasonCode.INSTRUMENT_UNKNOWN,),
                decided_at=now,
                audit=audit,
            )
        if any(
            leg.side is Side.SELL for leg in intent.legs
        ) and not _short_is_admissible(structure, policy):
            return self._reject(
                intent,
                portfolio,
                reason_codes=(ReasonCode.RISK_LIMIT_TRADE,),
                applied_limits=("naked_short_disabled",),
                decided_at=now,
                audit=audit,
            )
        if _is_multi_leg(structure):
            spread_reason = _multi_leg_spread_reason(
                request.leg_snapshots,
                intent.entry_policy.max_spread,
            )
        else:
            spread_reason = _spread_reason(feature, intent.entry_policy.max_spread)
        if spread_reason is not None:
            return self._reject(
                intent,
                portfolio,
                reason_codes=(spread_reason,),
                decided_at=now,
                audit=audit,
            )
        instrument_reason = _instrument_reason(request.instrument, structure)
        if instrument_reason is not None:
            return self._reject(
                intent,
                portfolio,
                reason_codes=(instrument_reason,),
                decided_at=now,
                audit=audit,
            )
        if _is_multi_leg(structure):
            bundle = validate_leg_snapshot_bundle(
                intent,
                request.leg_snapshots,
                now=now,
                freshness=self._account_config.config.freshness,
            )
            audit = _SnapshotAudit(
                decision_snapshot_id=bundle.decision_snapshot_id,
                decision_timestamp=bundle.decision_timestamp,
                leg_quotes=bundle.leg_quotes,
            )
            if bundle.reason is not None:
                return self._reject(
                    intent,
                    portfolio,
                    reason_codes=(bundle.reason,),
                    decided_at=now,
                    audit=audit,
                )

        if not policy.has_allocation(intent.strategy_id):
            # Fail closed: build_sizing_limits raises for an unlisted strategy,
            # and a configuration gap must surface as a machine-readable
            # rejection rather than an exception out of the decision path.
            return self._reject(
                intent,
                portfolio,
                reason_codes=(ReasonCode.CAPITAL_UNAVAILABLE,),
                applied_limits=("no_strategy_allocation",),
                decided_at=now,
                audit=audit,
            )

        limits = build_sizing_limits(
            portfolio,
            account_risk,
            policy,
            intent.strategy_id,
            config_version=self._account_config.version,
        )
        sizing_request = SizingRequest(
            request_id=self._ids.new_id("SIZE-REQ"),
            intent=intent,
            feature_snapshot=feature,
            portfolio_snapshot=portfolio,
            limits=limits,
            requested_at=now,
        )
        try:
            sizing = _compute_sizing_outcome(
                structure,
                feature,
                sizing_request,
                request,
                policy=policy,
                account_risk=account_risk,
                long_option_sizer=self._long_option_sizer,
                debit_spread_sizer=self._debit_spread_sizer,
                credit_spread_sizer=self._credit_spread_sizer,
                iron_condor_sizer=self._iron_condor_sizer,
                commodity_future_sizer=self._commodity_future_sizer,
                margin_preview=self._margin_preview,
                account_id=self._account_config.config.account_id,
                preview_request_id=self._ids.new_id("MARGIN-PREV"),
            )
        except _SizingError:
            return self._reject(
                intent,
                portfolio,
                reason_codes=(ReasonCode.PRICE_UNAVAILABLE,),
                decided_at=now,
                audit=audit,
            )
        if sizing.approved_lots <= 0:
            reason = _zero_lot_reason(sizing.bounds)
            return self._reject(
                intent,
                portfolio,
                reason_codes=(reason,),
                decided_at=now,
                audit=audit,
            )

        limit_check = evaluate_pre_trade_limits(
            intent,
            portfolio,
            account_risk,
            policy,
            limits,
            recalculated_max_loss=sizing.recalculated_max_loss,
            approved_lots=sizing.approved_lots,
        )
        if not limit_check.passed:
            return self._reject(
                intent,
                portfolio,
                reason_codes=limit_check.reason_codes,
                applied_limits=limit_check.applied_limits,
                decided_at=now,
                audit=audit,
            )

        decision_id = self._ids.new_id("DEC")
        reservation = self._reservations.try_reserve(
            intent_id=intent.intent_id,
            strategy_id=intent.strategy_id,
            amount=sizing.recalculated_max_loss,
            margin_available=limits.margin_available,
            risk_decision_id=decision_id,
        )
        if reservation.state is not ReservationState.RESERVED:
            reason = (
                reservation.reason_codes[0]
                if reservation.reason_codes
                else ReasonCode.CAPITAL_UNAVAILABLE
            )
            return self._reject(
                intent,
                portfolio,
                reason_codes=(reason,),
                decided_at=now,
                audit=audit,
            )

        post_trade = project_post_trade_exposure(
            portfolio,
            margin_required=sizing.estimated_margin,
            premium_paid=sizing.recalculated_max_loss,
            net_delta_delta=sizing.net_delta_delta,
        )
        action = (
            RiskAction.RESIZE
            if sizing.recalculated_max_loss < intent.requested_risk
            else RiskAction.APPROVE
        )
        expires_at = now + timedelta(seconds=policy.decision_ttl_seconds)
        return RiskDecision(
            decision_id=decision_id,
            intent_id=intent.intent_id,
            correlation_id=intent.correlation_id,
            experiment_id=intent.experiment_id,
            execution_mode=intent.execution_mode,
            policy_version=policy.policy_version,
            config_version=self._account_config.version,
            action=action,
            approved_legs=sizing.approved_legs,
            capital_reservation_id=reservation.reservation_id,
            reserved_capital=reservation.amount,
            recalculated_max_loss=sizing.recalculated_max_loss,
            margin_required=sizing.estimated_margin,
            pre_trade_exposure=pre_trade,
            post_trade_projection=post_trade,
            applied_limits=limit_check.applied_limits,
            reason_codes=limit_check.reason_codes,
            decided_at=now,
            expires_at=expires_at,
            decision_snapshot_id=audit["decision_snapshot_id"],
            decision_timestamp=audit["decision_timestamp"],
            leg_quotes=audit["leg_quotes"],
        )

    def _reject(
        self,
        intent: TradeIntent,
        portfolio: PortfolioSnapshot,
        *,
        reason_codes: tuple[ReasonCode, ...],
        decided_at: datetime,
        applied_limits: tuple[str, ...] = (),
        audit: _SnapshotAudit | None = None,
    ) -> RiskDecision:
        policy = self._risk_policy.config
        expires_at = decided_at + timedelta(seconds=policy.decision_ttl_seconds)
        extra_snapshot_id = audit["decision_snapshot_id"] if audit is not None else None
        extra_timestamp = audit["decision_timestamp"] if audit is not None else None
        extra_quotes = audit["leg_quotes"] if audit is not None else ()
        return RiskDecision(
            decision_id=self._ids.new_id("DEC"),
            intent_id=intent.intent_id,
            correlation_id=intent.correlation_id,
            experiment_id=intent.experiment_id,
            execution_mode=intent.execution_mode,
            policy_version=policy.policy_version,
            config_version=self._account_config.version,
            action=RiskAction.REJECT,
            pre_trade_exposure=portfolio.exposure,
            applied_limits=applied_limits,
            reason_codes=reason_codes,
            decided_at=decided_at,
            expires_at=expires_at,
            decision_snapshot_id=extra_snapshot_id,
            decision_timestamp=extra_timestamp,
            leg_quotes=extra_quotes,
        )


def _detect_structure(request: RiskGatewayRequest) -> _StructureKind | None:
    intent = request.intent
    if is_iron_condor(intent):
        return _StructureKind.IRON_CONDOR
    if is_debit_spread(intent):
        return _StructureKind.DEBIT_SPREAD
    if is_credit_spread(intent):
        return _StructureKind.CREDIT_SPREAD
    if is_commodity_future(intent, request.instrument):
        return _StructureKind.COMMODITY_FUTURE
    if (
        len(intent.legs) == 1
        and intent.legs[0].side is Side.BUY
        and request.instrument.instrument_kind is InstrumentKind.OPTION
    ):
        return _StructureKind.LONG_OPTION
    return None


def _constraint_reason(
    request: RiskGatewayRequest,
    *,
    now: datetime,
) -> ReasonCode | None:
    """Revalidate strategy entry constraints against decision-time evidence."""
    constraints = request.intent.constraints
    if constraints.require_event_blackout_clear:
        event_risk = request.event_risk_state
        if (
            event_risk is None
            or event_risk.scope not in {request.intent.underlying, "GLOBAL"}
            or now < event_risk.as_of
            or now >= event_risk.expires_at
            or event_risk.quality_state is not NewsQuality.VALID
            or event_risk.state not in {EventRiskStatus.NORMAL, EventRiskStatus.CAUTION}
        ):
            return ReasonCode.EVENT_BLACKOUT

    if request.leg_snapshots:
        snapshots = tuple(request.leg_snapshots.values())
    else:
        snapshots = (request.feature_snapshot,)
    for snapshot in snapshots:
        derivatives = snapshot.derivatives
        if derivatives is None:
            return ReasonCode.INSTRUMENT_UNKNOWN
        if derivatives.days_to_expiry < constraints.min_days_to_expiry:
            return ReasonCode.CONTRACT_EXPIRED
        minimum_oi = constraints.min_open_interest
        if minimum_oi is not None and (
            derivatives.open_interest is None or derivatives.open_interest < minimum_oi
        ):
            return ReasonCode.DEPTH_INSUFFICIENT
    return None


def _is_defined_risk(structure: _StructureKind) -> bool:
    return structure in {
        _StructureKind.DEBIT_SPREAD,
        _StructureKind.CREDIT_SPREAD,
        _StructureKind.IRON_CONDOR,
    }


def _short_is_admissible(structure: _StructureKind, policy: RiskPolicyConfig) -> bool:
    """Whether an intent carrying a SELL leg may be admitted.

    Two ways a short is allowed, and nothing else:

      - its worst case is capped by construction (spreads, iron condor); or
      - it is a single-leg commodity future whose loss is bounded by the same
        mandatory protective stop that sized it, so the position cannot lose
        more than the risk already approved for it. This is a policy switch,
        off by default, and it admits that one structure only.

    Naked short options are never admissible on either path.
    """
    if _is_defined_risk(structure):
        return True
    return (
        structure is _StructureKind.COMMODITY_FUTURE
        and policy.allow_stop_bounded_futures_short
    )


def _is_multi_leg(structure: _StructureKind) -> bool:
    return structure in {
        _StructureKind.DEBIT_SPREAD,
        _StructureKind.CREDIT_SPREAD,
        _StructureKind.IRON_CONDOR,
    }


def _instrument_reason(
    instrument: InstrumentSpec,
    structure: _StructureKind,
) -> ReasonCode | None:
    if structure is _StructureKind.COMMODITY_FUTURE:
        if instrument.instrument_kind is not InstrumentKind.FUTURE:
            return ReasonCode.INSTRUMENT_UNKNOWN
        return None
    if instrument.instrument_kind is not InstrumentKind.OPTION:
        return ReasonCode.INSTRUMENT_UNKNOWN
    return None


def _compute_sizing_outcome(
    structure: _StructureKind,
    feature: FeatureSnapshot,
    sizing_request: SizingRequest,
    request: RiskGatewayRequest,
    *,
    policy: RiskPolicyConfig,
    account_risk: RiskLimits,
    long_option_sizer: LongOptionSizingEngine,
    debit_spread_sizer: DebitSpreadSizingEngine,
    credit_spread_sizer: CreditSpreadSizingEngine,
    iron_condor_sizer: IronCondorSizingEngine,
    commodity_future_sizer: CommodityFutureSizingEngine,
    margin_preview: MarginPreviewPort,
    account_id: str,
    preview_request_id: str,
) -> _SizingOutcome:
    lot_size = LotSize(request.instrument.lot_size)
    if structure is _StructureKind.DEBIT_SPREAD:
        try:
            spread_sizing = debit_spread_sizer.size(
                sizing_request,
                request.leg_snapshots,
                policy,
                margin_preview,
                account_id=account_id,
                account_risk=account_risk,
                preview_request_id=preview_request_id,
                lot_size=lot_size,
            )
        except ValueError as exc:
            raise _SizingError(str(exc)) from exc
        approved_lots = spread_sizing.approved_lots
        approved_legs = _approved_spread_legs(
            request.intent,
            approved_lots,
            spread_sizing.lot_size,
        )
        net_delta_delta = (
            _spread_net_delta_delta(
                request.leg_snapshots,
                approved_lots,
                spread_sizing.lot_size,
            )
            if approved_lots > 0
            else 0
        )
        return _SizingOutcome(
            approved_lots=approved_lots,
            bounds=spread_sizing.bounds,
            recalculated_max_loss=spread_sizing.recalculated_max_loss,
            estimated_margin=spread_sizing.estimated_margin,
            approved_legs=approved_legs,
            net_delta_delta=net_delta_delta,
        )

    if structure is _StructureKind.CREDIT_SPREAD:
        try:
            credit_sizing = credit_spread_sizer.size(
                sizing_request,
                request.leg_snapshots,
                policy,
                margin_preview,
                account_id=account_id,
                account_risk=account_risk,
                preview_request_id=preview_request_id,
                lot_size=lot_size,
            )
        except ValueError as exc:
            raise _SizingError(str(exc)) from exc
        approved_lots = credit_sizing.approved_lots
        approved_legs = _approved_credit_spread_legs(
            request.intent,
            approved_lots,
            credit_sizing.lot_size,
        )
        net_delta_delta = (
            _spread_net_delta_delta(
                request.leg_snapshots,
                approved_lots,
                credit_sizing.lot_size,
            )
            if approved_lots > 0
            else 0
        )
        return _SizingOutcome(
            approved_lots=approved_lots,
            bounds=credit_sizing.bounds,
            recalculated_max_loss=credit_sizing.recalculated_max_loss,
            estimated_margin=credit_sizing.estimated_margin,
            approved_legs=approved_legs,
            net_delta_delta=net_delta_delta,
        )

    if structure is _StructureKind.IRON_CONDOR:
        try:
            condor_sizing = iron_condor_sizer.size(
                sizing_request,
                request.leg_snapshots,
                policy,
                margin_preview,
                account_id=account_id,
                account_risk=account_risk,
                preview_request_id=preview_request_id,
                lot_size=lot_size,
            )
        except ValueError as exc:
            raise _SizingError(str(exc)) from exc
        approved_lots = condor_sizing.approved_lots
        approved_legs = _approved_condor_legs(
            condor_sizing.legs,
            approved_lots,
            condor_sizing.lot_size,
        )
        net_delta_delta = (
            _spread_net_delta_delta(
                request.leg_snapshots,
                approved_lots,
                condor_sizing.lot_size,
            )
            if approved_lots > 0
            else 0
        )
        return _SizingOutcome(
            approved_lots=approved_lots,
            bounds=condor_sizing.bounds,
            recalculated_max_loss=condor_sizing.recalculated_max_loss,
            estimated_margin=condor_sizing.estimated_margin,
            approved_legs=approved_legs,
            net_delta_delta=net_delta_delta,
        )

    if structure is _StructureKind.COMMODITY_FUTURE:
        try:
            future_sizing = commodity_future_sizer.size(
                sizing_request,
                request.instrument,
                policy,
                margin_preview,
                account_id=account_id,
                account_risk=account_risk,
                preview_request_id=preview_request_id,
            )
        except ValueError as exc:
            raise _SizingError(str(exc)) from exc
        approved_lots = future_sizing.approved_lots
        leg = request.intent.legs[0]
        approved_legs = (
            (
                ApprovedLeg(
                    leg_id=leg.leg_id,
                    lots=Lots(approved_lots),
                    lot_size=future_sizing.lot_size,
                ),
            )
            if approved_lots > 0
            else ()
        )
        return _SizingOutcome(
            approved_lots=approved_lots,
            bounds=future_sizing.bounds,
            recalculated_max_loss=future_sizing.recalculated_max_loss,
            estimated_margin=future_sizing.estimated_margin,
            approved_legs=approved_legs,
            net_delta_delta=(
                _net_delta_delta(feature, approved_lots, future_sizing.lot_size)
                if approved_lots > 0
                else 0
            ),
        )

    option_sizing = long_option_sizer.size(
        sizing_request,
        request.instrument,
        policy,
        margin_preview,
        account_id=account_id,
        account_risk=account_risk,
        preview_request_id=preview_request_id,
    )
    approved_lots = option_sizing.approved_lots
    leg = request.intent.legs[0]
    approved_legs = (
        (
            ApprovedLeg(
                leg_id=leg.leg_id,
                lots=Lots(approved_lots),
                lot_size=option_sizing.lot_size,
            ),
        )
        if approved_lots > 0
        else ()
    )
    return _SizingOutcome(
        approved_lots=approved_lots,
        bounds=option_sizing.bounds,
        recalculated_max_loss=option_sizing.recalculated_max_loss,
        estimated_margin=option_sizing.estimated_margin,
        approved_legs=approved_legs,
        net_delta_delta=(
            _net_delta_delta(feature, approved_lots, option_sizing.lot_size)
            if approved_lots > 0
            else 0
        ),
    )


def _approved_spread_legs(
    intent: TradeIntent,
    approved_lots: int,
    lot_size: LotSize,
) -> tuple[ApprovedLeg, ...]:
    if approved_lots <= 0:
        return ()
    long_leg, short_leg = spread_legs(intent)
    return (
        ApprovedLeg(
            leg_id=long_leg.leg_id,
            lots=Lots(approved_lots),
            lot_size=lot_size,
        ),
        ApprovedLeg(
            leg_id=short_leg.leg_id,
            lots=Lots(approved_lots),
            lot_size=lot_size,
        ),
    )


def _approved_credit_spread_legs(
    intent: TradeIntent,
    approved_lots: int,
    lot_size: LotSize,
) -> tuple[ApprovedLeg, ...]:
    if approved_lots <= 0:
        return ()
    short_leg, long_leg = credit_spread_legs(intent)
    return (
        ApprovedLeg(
            leg_id=short_leg.leg_id,
            lots=Lots(approved_lots),
            lot_size=lot_size,
        ),
        ApprovedLeg(
            leg_id=long_leg.leg_id,
            lots=Lots(approved_lots),
            lot_size=lot_size,
        ),
    )


def _approved_condor_legs(
    legs: object,
    approved_lots: int,
    lot_size: LotSize,
) -> tuple[ApprovedLeg, ...]:
    if approved_lots <= 0:
        return ()
    if not isinstance(legs, IronCondorLegs):
        raise TypeError("legs must be IronCondorLegs")
    return tuple(
        ApprovedLeg(
            leg_id=leg.leg_id,
            lots=Lots(approved_lots),
            lot_size=lot_size,
        )
        for leg in (
            legs.short_call,
            legs.long_call,
            legs.short_put,
            legs.long_put,
        )
    )


def _quality_reason(state: DataQuality) -> ReasonCode:
    if state is DataQuality.STALE:
        return ReasonCode.DATA_STALE
    if state is DataQuality.INVALID:
        return ReasonCode.DATA_INVALID
    return ReasonCode.DATA_DEGRADED


def _spread_reason(feature: FeatureSnapshot, max_spread: Percent) -> ReasonCode | None:
    bid = feature.market.bid
    ask = feature.market.ask
    if bid is None or ask is None:
        return ReasonCode.PRICE_UNAVAILABLE
    mid = (bid.value + ask.value) / 2
    if mid <= 0:
        return ReasonCode.PRICE_UNAVAILABLE
    spread_fraction = (ask.value - bid.value) / mid
    if spread_fraction > max_spread.fraction:
        return ReasonCode.SPREAD_TOO_WIDE
    return None


def _zero_lot_reason(bounds: LotBounds) -> ReasonCode:
    if bounds.margin_lots <= 0:
        return ReasonCode.MARGIN_INSUFFICIENT
    if bounds.capital_lots <= 0:
        return ReasonCode.CAPITAL_UNAVAILABLE
    if bounds.risk_lots <= 0:
        return ReasonCode.RISK_LIMIT_TRADE
    if bounds.portfolio_limit_lots <= 0:
        return ReasonCode.RISK_LIMIT_PORTFOLIO
    if bounds.liquidity_lots <= 0:
        return ReasonCode.DEPTH_INSUFFICIENT
    return ReasonCode.SIZE_BELOW_MINIMUM


def _net_delta_delta(
    feature: FeatureSnapshot,
    approved_lots: int,
    lot_size: LotSize,
) -> int:
    derivatives = feature.derivatives
    contracts = approved_lots * lot_size.contracts_per_lot
    if derivatives is None or derivatives.greeks is None:
        return contracts
    delta = derivatives.greeks.delta
    if delta is None:
        return contracts
    return int(Decimal(delta) * Decimal(contracts))


def _spread_net_delta_delta(
    leg_snapshots: Mapping[str, FeatureSnapshot],
    approved_lots: int,
    lot_size: LotSize,
) -> int:
    contracts = approved_lots * lot_size.contracts_per_lot
    total = Decimal(0)
    for snapshot in leg_snapshots.values():
        derivatives = snapshot.derivatives
        if derivatives is None or derivatives.greeks is None:
            continue
        delta = derivatives.greeks.delta
        if delta is None:
            continue
        total += Decimal(delta) * Decimal(contracts)
    if total == 0:
        return contracts
    return int(total)


def _primary_audit(
    intent: TradeIntent,
    feature: FeatureSnapshot,
    leg_snapshots: Mapping[str, FeatureSnapshot],
    now: datetime,
) -> _SnapshotAudit:
    quotes: list[LegQuoteRef] = []
    for leg in intent.legs:
        snapshot = leg_snapshots.get(leg.leg_id)
        if snapshot is None:
            continue
        quotes.append(
            LegQuoteRef(
                leg_id=leg.leg_id,
                snapshot_id=snapshot.snapshot_id,
                symbol=snapshot.contract.symbol,
                event_time=snapshot.times.event_time,
                calculation_time=snapshot.times.calculation_time,
            )
        )
    if not quotes:
        quotes.append(
            LegQuoteRef(
                leg_id=intent.legs[0].leg_id if intent.legs else "primary",
                snapshot_id=feature.snapshot_id,
                symbol=feature.contract.symbol,
                event_time=feature.times.event_time,
                calculation_time=feature.times.calculation_time,
            )
        )
    return _SnapshotAudit(
        decision_snapshot_id=intent.snapshot_id,
        decision_timestamp=now,
        leg_quotes=tuple(quotes),
    )


def _multi_leg_spread_reason(
    leg_snapshots: Mapping[str, FeatureSnapshot],
    max_spread: Percent,
) -> ReasonCode | None:
    for snapshot in leg_snapshots.values():
        reason = _spread_reason(snapshot, max_spread)
        if reason is not None:
            return reason
    return None
