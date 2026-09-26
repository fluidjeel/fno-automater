"""Vertical debit spread (bull call / bear put) — Layer 3 slice 2.

A defined-risk, premium-paid two-leg structure: buy the nearer-money option and
sell the further out-of-the-money option of the same type and expiry. The short
leg is never naked; it is covered by the long leg. The strategy emits one
``TradeIntent`` with two ratio legs; Layer 2 sizes and prices it.

The legs are only built when the candidate pair can express the resolved
direction. A bullish read over put candidates would otherwise order the
opposite structure, so that case yields no intent instead of a trade against
the signal.
"""

from __future__ import annotations

import hashlib
from datetime import timedelta
from decimal import Decimal

from trading.domain.contracts import (
    ContractRef,
    EntryPolicy,
    ExitTemplate,
    FeatureSnapshot,
    IntentConstraints,
    IntentLeg,
    TradeIntent,
)
from trading.domain.enums import (
    FamilyId,
    InstrumentKind,
    ModeId,
    OptionType,
    ReasonCode,
    Side,
)
from trading.domain.primitives import Currency, Money, Percent
from trading.strategies._common import DEFAULT_MACRO_MIN_CONFIDENCE, resolve_direction
from trading.strategies.base import (
    Rejection,
    StrategyContext,
    StrategyDecision,
    register_strategy,
)
from trading.strategies.macro import MacroBias
from trading.strategies.quote_freshness import quote_freshness_for_context

__all__ = ["DebitSpreadStrategy"]

STRATEGY_ID = "debit_spread"
STRATEGY_VERSION = "debit-spread-v1"

REQUESTED_RISK_INR = Decimal("10000")
MIN_DAYS_TO_EXPIRY = 7
MIN_OPEN_INTEREST = 1000
MAX_ENTRY_SPREAD_FRACTION = Decimal("0.05")
STOP_TICKS = 40
TARGET_TICKS = 80
EXIT_BEFORE_EXPIRY_DAYS = 1
ENTRY_TIMEOUT_SECONDS = 30
INTENT_TTL_SECONDS = 300
SESSION_LABEL = "NSE_FO"
INVALIDATION_NOTE = "Defined-risk vertical debit spread; maximum loss is the net debit."
SPREAD_LEG_COUNT = 2

_CURRENCY = Currency.INR


def _strike(option: FeatureSnapshot) -> Decimal:
    strike = option.contract.strike
    if strike is None:
        raise ValueError("option contract missing strike")
    return strike


@register_strategy
class DebitSpreadStrategy:
    """Bull call spread on a bullish read, bear put spread on a bearish read."""

    strategy_id = STRATEGY_ID
    strategy_version = STRATEGY_VERSION

    def evaluate(self, ctx: StrategyContext) -> StrategyDecision:  # noqa: PLR0911
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

        freshness = quote_freshness_for_context(ctx)
        if freshness.hard_reason is not None:
            return self._reject(
                decision,
                ctx,
                freshness.hard_reason,
                freshness.detail or "quote freshness check failed",
            )
        strict_would_block = freshness.strict_would_block

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
            return self._reject(
                decision, ctx, ReasonCode.DIRECTION_NEUTRAL, "direction neutral"
            )

        option_type = OptionType.CALL if bias is MacroBias.BULLISH else OptionType.PUT
        mismatched = any(
            option.contract.option_type is not option_type for option in ctx.candidates
        )
        if mismatched:
            return self._reject(
                decision,
                ctx,
                ReasonCode.OPTION_TYPE_MISMATCH,
                "candidate legs do not match directional read",
            )

        long_leg, short_leg = self._ordered_legs(ctx.candidates, option_type)
        intent = self._build_intent(ctx, long_leg, short_leg, option_type, confidence)
        return StrategyDecision(
            strategy_id=self.strategy_id,
            strategy_version=self.strategy_version,
            snapshot_id=ctx.underlying.snapshot_id,
            as_of=ctx.now,
            intents=(intent,),
            rejections=(),
            strict_would_block=strict_would_block,
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
        option_type = ctx.candidates[0].contract.option_type
        return self._structure_reason(ctx, option_type)

    def _structure_reason(
        self, ctx: StrategyContext, option_type: OptionType | None
    ) -> ReasonCode | None:
        first, second = ctx.candidates
        if first.contract.expiry != second.contract.expiry:
            return ReasonCode.INSTRUMENT_UNKNOWN
        if first.contract.underlying != second.contract.underlying:
            return ReasonCode.INSTRUMENT_UNKNOWN
        if (
            first.contract.option_type is not option_type
            or second.contract.option_type is not option_type
        ):
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
            derivatives.open_interest is None
            or derivatives.open_interest < MIN_OPEN_INTEREST
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

    @staticmethod
    def _ordered_legs(
        candidates: tuple[FeatureSnapshot, ...], option_type: OptionType
    ) -> tuple[IntentLeg, IntentLeg]:
        low, high = sorted(candidates, key=_strike)
        if _strike(low) >= _strike(high):
            raise ValueError("spread strikes must be distinct")
        if option_type is OptionType.CALL:
            long_leg, short_leg = low, high
        else:
            long_leg, short_leg = high, low
        return (
            IntentLeg(
                leg_id="leg-long", contract=long_leg.contract, side=Side.BUY, ratio=1
            ),
            IntentLeg(
                leg_id="leg-short", contract=short_leg.contract, side=Side.SELL, ratio=1
            ),
        )

    def _build_intent(
        self,
        ctx: StrategyContext,
        long_leg: IntentLeg,
        short_leg: IntentLeg,
        option_type: OptionType,
        confidence: Decimal,
    ) -> TradeIntent:
        risk = Money.of(REQUESTED_RISK_INR, _CURRENCY)
        setup_code = (
            "BULL_CALL_SPREAD" if option_type is OptionType.CALL else "BEAR_PUT_SPREAD"
        )
        now = ctx.now
        contract: ContractRef = long_leg.contract
        intent_id = _derive_id(
            self.strategy_id,
            self.strategy_version,
            ctx.underlying.snapshot_id,
            contract.symbol,
            setup_code,
            now.isoformat(),
        )

        return TradeIntent(
            intent_id=intent_id,
            correlation_id=_derive_id(self.strategy_id, ctx.underlying.snapshot_id),
            strategy_id=self.strategy_id,
            strategy_version=self.strategy_version,
            snapshot_id=ctx.underlying.snapshot_id,
            experiment_id=ctx.experiment_id,
            execution_mode=ctx.execution_mode,
            promoted_config_version=ctx.underlying.lineage.versions.config_version,
            promoted_proposal_id=None,
            supersedes_intent_id=None,
            mode_id=ModeId.M3_TACTICAL_POSITIONAL,
            family_id=(
                FamilyId.bull_call_debit
                if option_type is OptionType.CALL
                else FamilyId.bear_put_debit
            ),
            underlying=ctx.underlying.contract.underlying,
            asset_class=ctx.underlying.contract.asset_class,
            legs=(long_leg, short_leg),
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
                partial_fill_policy="ALL_OR_CANCEL",
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
