"""TradeIntent to RiskDecision orchestration. Invariant 4."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from trading.broker.ports import MarginPreviewPort
from trading.config.loader import LoadedConfig
from trading.config.risk_policy import LoadedRiskPolicy
from trading.domain.clock import Clock
from trading.domain.contracts.instrument import InstrumentSpec
from trading.domain.contracts.intent import TradeIntent
from trading.domain.contracts.portfolio import PortfolioSnapshot
from trading.domain.contracts.risk import ApprovedLeg, RiskDecision
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
from trading.domain.primitives import Lots, LotSize, Percent
from trading.risk.limits import (
    build_sizing_limits,
    evaluate_pre_trade_limits,
    project_post_trade_exposure,
)
from trading.risk.reservation import CapitalReservationService
from trading.risk.sizing.long_option import LongOptionSizingEngine, LotBounds

__all__ = ["RiskGateway", "RiskGatewayRequest"]


@dataclass(frozen=True, slots=True)
class RiskGatewayRequest:
    """Inputs required to evaluate one intent."""

    intent: TradeIntent
    feature_snapshot: FeatureSnapshot
    portfolio_snapshot: PortfolioSnapshot
    instrument: InstrumentSpec


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
        self._sizer = LongOptionSizingEngine()

    def evaluate(self, request: RiskGatewayRequest) -> RiskDecision:
        """Return an approval with reserved capital or a machine-readable rejection."""
        now = self._clock.now_utc()
        intent = request.intent
        portfolio = request.portfolio_snapshot
        feature = request.feature_snapshot
        policy = self._risk_policy.config
        account_risk = self._account_config.config.risk
        pre_trade = portfolio.exposure

        if not intent.is_live_at(now):
            return self._reject(
                intent,
                portfolio,
                reason_codes=(ReasonCode.DECISION_EXPIRED,),
                decided_at=now,
            )
        if intent.snapshot_id != feature.snapshot_id:
            return self._reject(
                intent,
                portfolio,
                reason_codes=(ReasonCode.SNAPSHOT_MISMATCH,),
                decided_at=now,
            )
        if not feature.permits_new_exposure:
            code = _quality_reason(feature.quality.state)
            return self._reject(intent, portfolio, reason_codes=(code,), decided_at=now)
        if any(leg.side is Side.SELL for leg in intent.legs):
            return self._reject(
                intent,
                portfolio,
                reason_codes=(ReasonCode.RISK_LIMIT_TRADE,),
                applied_limits=("naked_short_disabled",),
                decided_at=now,
            )
        spread_reason = _spread_reason(feature, intent.entry_policy.max_spread)
        if spread_reason is not None:
            return self._reject(
                intent,
                portfolio,
                reason_codes=(spread_reason,),
                decided_at=now,
            )
        if request.instrument.instrument_kind is not InstrumentKind.OPTION:
            return self._reject(
                intent,
                portfolio,
                reason_codes=(ReasonCode.INSTRUMENT_UNKNOWN,),
                decided_at=now,
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
        sizing = self._sizer.size(
            sizing_request,
            request.instrument,
            policy,
            self._margin_preview,
            account_id=self._account_config.config.account_id,
            account_risk=account_risk,
            preview_request_id=self._ids.new_id("MARGIN-PREV"),
        )

        if sizing.approved_lots <= 0:
            reason = _zero_lot_reason(sizing.bounds)
            return self._reject(
                intent,
                portfolio,
                reason_codes=(reason,),
                decided_at=now,
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
            )

        leg = intent.legs[0]
        approved_legs = (
            ApprovedLeg(
                leg_id=leg.leg_id,
                lots=Lots(sizing.approved_lots),
                lot_size=sizing.lot_size,
            ),
        )
        net_delta_delta = _net_delta_delta(
            feature,
            sizing.approved_lots,
            sizing.lot_size,
        )
        post_trade = project_post_trade_exposure(
            portfolio,
            margin_required=sizing.estimated_margin,
            premium_paid=sizing.recalculated_max_loss,
            net_delta_delta=net_delta_delta,
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
            policy_version=policy.policy_version,
            config_version=self._account_config.version,
            action=action,
            approved_legs=approved_legs,
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
        )

    def _reject(
        self,
        intent: TradeIntent,
        portfolio: PortfolioSnapshot,
        *,
        reason_codes: tuple[ReasonCode, ...],
        decided_at: datetime,
        applied_limits: tuple[str, ...] = (),
    ) -> RiskDecision:
        policy = self._risk_policy.config
        expires_at = decided_at + timedelta(seconds=policy.decision_ttl_seconds)
        return RiskDecision(
            decision_id=self._ids.new_id("DEC"),
            intent_id=intent.intent_id,
            correlation_id=intent.correlation_id,
            policy_version=policy.policy_version,
            config_version=self._account_config.version,
            action=RiskAction.REJECT,
            pre_trade_exposure=portfolio.exposure,
            applied_limits=applied_limits,
            reason_codes=reason_codes,
            decided_at=decided_at,
            expires_at=expires_at,
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
