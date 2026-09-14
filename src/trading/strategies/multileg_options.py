"""Defined-risk multi-leg options: credit spreads — Layer 3 slice 5.

A premium-receiving, defined-risk two-leg structure: sell the nearer-money
option and buy the further out-of-the-money option of the same type and expiry
as protection.

  - bull put spread on a bullish read  (buy the lower put,  sell the higher put)
  - bear call spread on a bearish read (sell the lower call, buy the higher call)

The protective wing is what makes the structure defined-risk: the short leg is
never naked, so the worst case is always statable and ``estimated_max_loss`` can
be set to ``requested_risk``. Legs are emitted in strike order with explicit
ratios; Layer 2 alone sizes and prices them.
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
from trading.domain.enums import InstrumentKind, OptionType, ReasonCode, Side
from trading.domain.primitives import Currency, Money, Percent
from trading.strategies._common import DEFAULT_MACRO_MIN_CONFIDENCE, resolve_direction
from trading.strategies.base import (
    Rejection,
    StrategyContext,
    StrategyDecision,
    register_strategy,
)
from trading.strategies.macro import MacroBias

__all__ = ["MultiLegOptionsStrategy"]

STRATEGY_ID = "defined_risk_multileg"
STRATEGY_VERSION = "defined-risk-multileg-v1"

# Versioned research parameters. These are uncalibrated and must not be treated
# as validated risk policy.
REQUESTED_RISK_INR = Decimal("10000")
MIN_DAYS_TO_EXPIRY = 7
MIN_OPEN_INTEREST = 1000
MAX_SNAPSHOT_AGE_SECONDS = 120
MAX_ENTRY_SPREAD_FRACTION = Decimal("0.05")
STOP_TICKS = 40
TARGET_TICKS = 40
EXIT_BEFORE_EXPIRY_DAYS = 1
ENTRY_TIMEOUT_SECONDS = 30
INTENT_TTL_SECONDS = 300
SESSION_LABEL = "NSE_FO"
SPREAD_LEG_COUNT = 2
SHORT_LEG_COUNT = 1
PARTIAL_FILL_POLICY = "ALL_OR_CANCEL"
INVALIDATION_NOTE = (
    "Defined-risk credit spread; the long wing caps the loss at the strike width."
)

_CURRENCY = Currency.INR


def _strike(option: FeatureSnapshot) -> Decimal:
    strike = option.contract.strike
    if strike is None:
        raise ValueError("option contract missing strike")
    return strike


def _option_type_for(bias: MacroBias) -> OptionType:
    """A bullish read sells puts; a bearish read sells calls."""
    return OptionType.PUT if bias is MacroBias.BULLISH else OptionType.CALL


def _leg(option: FeatureSnapshot, side: Side) -> IntentLeg:
    """One ratio leg, named for its role so the id is stable and readable."""
    role = "long" if side is Side.BUY else "short"
    return IntentLeg(leg_id=f"leg-{role}", contract=option.contract, side=side, ratio=1)


def _ordered_legs(
    candidates: tuple[FeatureSnapshot, ...], option_type: OptionType
) -> tuple[IntentLeg, IntentLeg]:
    """Strike-ascending legs with explicit credit-spread roles.

    This mirrors ``debit_spread._ordered_legs`` in how it picks by strike, but
    the roles invert: the short leg is the nearer-money strike and the long leg
    is the protective wing. The long wing is therefore always further out of the
    money than the short strike, so a short leg can never be naked.
    """
    low, high = sorted(candidates, key=_strike)
    if _strike(low) >= _strike(high):
        raise ValueError("spread strikes must be distinct")
    if option_type is OptionType.PUT:
        return _leg(low, Side.BUY), _leg(high, Side.SELL)
    return _leg(low, Side.SELL), _leg(high, Side.BUY)


@register_strategy
class MultiLegOptionsStrategy:
    """Bull put spread on a bullish read, bear call spread on a bearish read."""

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
        if len(ctx.candidates) != SPREAD_LEG_COUNT:
            return self._reject(
                decision,
                ctx,
                ReasonCode.INSTRUMENT_UNKNOWN,
                "exactly two options required",
            )

        reason = self._validation_reason(ctx)
        if reason is not None:
            return self._reject(decision, ctx, reason, "input failed validation")

        bias, confidence = resolve_direction(
            ctx.underlying,
            ctx.macro,
            ctx.now,
            min_confidence=DEFAULT_MACRO_MIN_CONFIDENCE,
        )
        if bias is MacroBias.NEUTRAL:
            return decision  # no directional signal: no trade, not an error

        option_type = _option_type_for(bias)
        mismatched = any(
            option.contract.option_type is not option_type for option in ctx.candidates
        )
        if mismatched:
            return decision  # candidates do not match the read; skip, do not reject

        legs = _ordered_legs(ctx.candidates, option_type)
        intent = self._build_intent(ctx, legs, option_type, confidence)
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

    def _validation_reason(self, ctx: StrategyContext) -> ReasonCode | None:
        if not ctx.underlying.permits_new_exposure:
            return ReasonCode.DATA_INVALID
        if any(not leg.permits_new_exposure for leg in ctx.candidates):
            return ReasonCode.DATA_INVALID
        age = ctx.now - ctx.underlying.times.calculation_time
        if age > timedelta(seconds=MAX_SNAPSHOT_AGE_SECONDS):
            return ReasonCode.DATA_STALE
        if ctx.now < ctx.underlying.times.calculation_time:
            return ReasonCode.DATA_INVALID  # decision instant precedes the data
        return self._structure_reason(ctx)

    def _structure_reason(self, ctx: StrategyContext) -> ReasonCode | None:
        """Both wings must be the same underlying, expiry, type and distinct strikes."""
        first, second = ctx.candidates
        if first.contract.expiry != second.contract.expiry:
            return ReasonCode.INSTRUMENT_UNKNOWN
        if first.contract.underlying != second.contract.underlying:
            return ReasonCode.INSTRUMENT_UNKNOWN
        if first.contract.option_type is not second.contract.option_type:
            return ReasonCode.INSTRUMENT_UNKNOWN
        low, high = sorted(ctx.candidates, key=_strike)
        if _strike(low) >= _strike(high):
            return ReasonCode.INSTRUMENT_UNKNOWN
        for option in ctx.candidates:
            reason = self._leg_reason(option)
            if reason is not None:
                return reason
        return None

    def _leg_reason(self, option: FeatureSnapshot) -> ReasonCode | None:
        derivatives = option.derivatives
        if (
            option.contract.instrument_kind is not InstrumentKind.OPTION
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
        return self._spread_reason(option)

    @staticmethod
    def _spread_reason(option: FeatureSnapshot) -> ReasonCode | None:
        bid = option.market.bid
        ask = option.market.ask
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
        legs: tuple[IntentLeg, IntentLeg],
        option_type: OptionType,
        confidence: Decimal,
    ) -> TradeIntent:
        if sum(1 for leg in legs if leg.side is Side.SELL) != SHORT_LEG_COUNT:
            raise ValueError(
                "a credit spread must carry exactly one short leg, and it must be "
                "covered by the long wing (invariant 16)"
            )
        risk = Money.of(REQUESTED_RISK_INR, _CURRENCY)
        setup_code = (
            "BULL_PUT_SPREAD" if option_type is OptionType.PUT else "BEAR_CALL_SPREAD"
        )
        now = ctx.now
        intent_id = _derive_id(
            self.strategy_id,
            self.strategy_version,
            ctx.underlying.snapshot_id,
            "|".join(leg.contract.symbol for leg in legs),
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
            legs=legs,
            entry_policy=EntryPolicy(
                limit_offset_ticks=0,
                max_spread=Percent.from_fraction(MAX_ENTRY_SPREAD_FRACTION),
                max_slippage=Percent.from_percent(Decimal("0.25")),
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
                partial_fill_policy=PARTIAL_FILL_POLICY,
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
