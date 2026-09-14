"""Directional commodity futures — Layer 3 slice 3.

A single futures leg in the direction of the deterministic read (long on
bullish, short on bearish). Unlike options, futures have no structural loss
bound; the ``ExitTemplate`` stop is what bounds the loss, and Layer 2 sizes
lots so that the stop equals ``requested_risk``.
"""

from __future__ import annotations

import hashlib
from datetime import timedelta
from decimal import Decimal

from trading.domain.contracts import (
    EntryPolicy,
    ExitTemplate,
    FeatureSnapshot,
    IntentConstraints,
    IntentLeg,
    TradeIntent,
)
from trading.domain.enums import InstrumentKind, ReasonCode, Side
from trading.domain.primitives import Currency, Money, Percent
from trading.strategies._common import DEFAULT_MACRO_MIN_CONFIDENCE, resolve_direction
from trading.strategies.base import (
    Rejection,
    StrategyContext,
    StrategyDecision,
    register_strategy,
)
from trading.strategies.macro import MacroBias

__all__ = ["CommodityFuturesStrategy"]

STRATEGY_ID = "commodity_futures_trend"
STRATEGY_VERSION = "commodity-futures-v1"

REQUESTED_RISK_INR = Decimal("10000")
MIN_DAYS_TO_EXPIRY = 3
MIN_OPEN_INTEREST = 500
MAX_SNAPSHOT_AGE_SECONDS = 120
MAX_ENTRY_SPREAD_FRACTION = Decimal("0.02")
STOP_TICKS = 60
TARGET_TICKS = 120
EXIT_BEFORE_EXPIRY_DAYS = 1
ENTRY_TIMEOUT_SECONDS = 30
INTENT_TTL_SECONDS = 300
SESSION_LABEL = "MCX"
INVALIDATION_NOTE = "Stop-bounded futures position; the stop is the maximum loss."

_CURRENCY = Currency.INR


@register_strategy
class CommodityFuturesStrategy:
    """Long futures on a bullish read, short futures on a bearish read."""

    strategy_id = STRATEGY_ID
    strategy_version = STRATEGY_VERSION

    def evaluate(self, ctx: StrategyContext) -> StrategyDecision:
        decision = StrategyDecision(
            strategy_id=self.strategy_id,
            strategy_version=self.strategy_version,
            snapshot_id=ctx.underlying.snapshot_id,
            as_of=ctx.now,
            intents=(),
            rejections=(),
        )

        if not ctx.view.entries_permitted:
            return self._reject(
                decision, ctx, ReasonCode.ENTRY_FROZEN, "entries not permitted"
            )
        if len(ctx.candidates) != 1:
            return self._reject(
                decision,
                ctx,
                ReasonCode.INSTRUMENT_UNKNOWN,
                "exactly one future required",
            )

        stale = self._snapshot_is_stale(ctx)
        if stale is not None:
            return self._reject(decision, ctx, stale, "snapshot is stale or invalid")

        future = ctx.candidates[0]
        eligibility = self._future_eligibility_reason(future)
        if eligibility is not None:
            return self._reject(
                decision, ctx, eligibility, "future contract ineligible"
            )

        bias, confidence = resolve_direction(
            ctx.underlying,
            ctx.macro,
            ctx.now,
            min_confidence=DEFAULT_MACRO_MIN_CONFIDENCE,
        )
        if bias is MacroBias.NEUTRAL:
            return decision

        side = Side.BUY if bias is MacroBias.BULLISH else Side.SELL
        intent = self._build_intent(ctx, future, side, bias, confidence)
        return StrategyDecision(
            strategy_id=self.strategy_id,
            strategy_version=self.strategy_version,
            snapshot_id=ctx.underlying.snapshot_id,
            as_of=ctx.now,
            intents=(intent,),
            rejections=(),
        )

    @staticmethod
    def _reject(
        decision: StrategyDecision,
        ctx: StrategyContext,
        reason: ReasonCode,
        detail: str,
    ) -> StrategyDecision:
        rejection = Rejection(
            reason=reason,
            detail=detail,
            snapshot_id=ctx.underlying.snapshot_id,
            as_of=ctx.now,
        )
        return StrategyDecision(
            strategy_id=decision.strategy_id,
            strategy_version=decision.strategy_version,
            snapshot_id=decision.snapshot_id,
            as_of=decision.as_of,
            intents=(),
            rejections=(*decision.rejections, rejection),
        )

    def _snapshot_is_stale(self, ctx: StrategyContext) -> ReasonCode | None:
        if not ctx.underlying.permits_new_exposure:
            return ReasonCode.DATA_INVALID
        if not ctx.candidates[0].permits_new_exposure:
            return ReasonCode.DATA_INVALID
        age = ctx.now - ctx.underlying.times.calculation_time
        if age > timedelta(seconds=MAX_SNAPSHOT_AGE_SECONDS):
            return ReasonCode.DATA_STALE
        if ctx.now < ctx.underlying.times.calculation_time:
            return ReasonCode.DATA_INVALID
        return None

    def _future_eligibility_reason(self, future: FeatureSnapshot) -> ReasonCode | None:
        derivatives = future.derivatives
        if (
            future.contract.instrument_kind is not InstrumentKind.FUTURE
            or derivatives is None
        ):
            return ReasonCode.INSTRUMENT_UNKNOWN
        if derivatives.days_to_expiry < MIN_DAYS_TO_EXPIRY:
            return ReasonCode.CONTRACT_EXPIRED
        if (
            derivatives.open_interest is not None
            and derivatives.open_interest < MIN_OPEN_INTEREST
        ):
            return ReasonCode.DEPTH_INSUFFICIENT
        return self._spread_reason(future)

    @staticmethod
    def _spread_reason(future: FeatureSnapshot) -> ReasonCode | None:
        bid = future.market.bid
        ask = future.market.ask
        if bid is None or ask is None:
            return ReasonCode.PRICE_UNAVAILABLE
        mid = (bid.value + ask.value) / 2
        if mid <= 0:
            return ReasonCode.PRICE_UNAVAILABLE
        if (ask.value - bid.value) / mid > MAX_ENTRY_SPREAD_FRACTION:
            return ReasonCode.SPREAD_TOO_WIDE
        return None

    def _build_intent(
        self,
        ctx: StrategyContext,
        future: FeatureSnapshot,
        side: Side,
        bias: MacroBias,
        confidence: Decimal,
    ) -> TradeIntent:
        risk = Money.of(REQUESTED_RISK_INR, _CURRENCY)
        setup_code = f"{side.value}_FUTURES_{bias.value}"
        now = ctx.now
        intent_id = _derive_id(
            self.strategy_id,
            self.strategy_version,
            ctx.underlying.snapshot_id,
            future.contract.symbol,
            setup_code,
            now.isoformat(),
        )

        return TradeIntent(
            intent_id=intent_id,
            correlation_id=_derive_id(self.strategy_id, ctx.underlying.snapshot_id),
            strategy_id=self.strategy_id,
            strategy_version=self.strategy_version,
            snapshot_id=ctx.underlying.snapshot_id,
            promoted_config_version=ctx.underlying.lineage.versions.config_version,
            promoted_proposal_id=None,
            supersedes_intent_id=None,
            underlying=ctx.underlying.contract.underlying,
            asset_class=ctx.underlying.contract.asset_class,
            legs=(
                IntentLeg(leg_id="leg-1", contract=future.contract, side=side, ratio=1),
            ),
            entry_policy=EntryPolicy(
                limit_offset_ticks=0,
                max_spread=Percent.from_fraction(MAX_ENTRY_SPREAD_FRACTION),
                max_slippage=Percent.from_percent(Decimal("0.20")),
                timeout_seconds=ENTRY_TIMEOUT_SECONDS,
                max_attempts=1,
                allow_market_fallback=False,
            ),
            exit_template=ExitTemplate(
                stop_distance_ticks=STOP_TICKS,
                target_distance_ticks=TARGET_TICKS,
                break_even_trigger_ticks=None,
                trailing_activation_ticks=None,
                trailing_distance_ticks=None,
                time_exit=None,
                exit_before_expiry_days=EXIT_BEFORE_EXPIRY_DAYS,
                invalidation_note=INVALIDATION_NOTE,
                partial_fill_policy="CANCEL_REMAINDER",
            ),
            constraints=IntentConstraints(
                min_days_to_expiry=MIN_DAYS_TO_EXPIRY,
                max_holding_days=None,
                require_event_blackout_clear=True,
                min_open_interest=MIN_OPEN_INTEREST,
                session_label=SESSION_LABEL,
            ),
            requested_risk=risk,
            estimated_max_loss=risk,
            setup_code=setup_code,
            strategy_confidence=confidence,
            created_at=now,
            expires_at=now + timedelta(seconds=INTENT_TTL_SECONDS),
        )


def _derive_id(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()
