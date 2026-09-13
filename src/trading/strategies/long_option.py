"""Positional long call / long put strategy (Layer 3 first vertical slice).

Defined-risk, premium-paid direction: buy one option contract whose type
matches a deterministic directional read. The strategy emits a single
``TradeIntent`` with a ratio leg (never a quantity) and a complete protective
``ExitTemplate``; Layer 2 alone sizes and prices it.

Determinism: every parameter is a versioned constant and ``intent_id`` is a
hash of the decision inputs, so identical inputs produce identical bytes.
No lookahead: only the injected ``now`` and snapshot timestamps are read.
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
from trading.strategies.base import (
    Rejection,
    StrategyContext,
    StrategyDecision,
    register_strategy,
)
from trading.strategies.macro import MacroBias, accepted_macro_bias

__all__ = ["LongOptionStrategy"]

STRATEGY_ID = "positional_long_option"
STRATEGY_VERSION = "long-option-v1"

# Versioned research parameters. These are uncalibrated and must not be treated
# as validated risk policy; changing any of them changes STRATEGY_VERSION.
REQUESTED_RISK_INR = Decimal("10000")
MIN_DAYS_TO_EXPIRY = 7
MIN_OPEN_INTEREST = 1000
MAX_SNAPSHOT_AGE_SECONDS = 120
MAX_ENTRY_SPREAD_FRACTION = Decimal("0.05")
MACRO_MIN_CONFIDENCE = Decimal("0.6")
TECHNICAL_CONFIDENCE = Decimal("0.5")
STOP_TICKS = 40
TARGET_TICKS = 80
EXIT_BEFORE_EXPIRY_DAYS = 1
ENTRY_TIMEOUT_SECONDS = 30
INTENT_TTL_SECONDS = 300
SESSION_LABEL = "NSE_FO"
INVALIDATION_NOTE = "Long premium position; maximum loss is the paid premium."

_CURRENCY = Currency.INR


def _technical_bias(ctx: StrategyContext) -> MacroBias:
    """Price above the previous close is bullish, below is bearish."""
    last = ctx.underlying.market.last
    close = ctx.underlying.market.close
    if last is None or close is None:
        return MacroBias.NEUTRAL
    if last.value > close.value:
        return MacroBias.BULLISH
    if last.value < close.value:
        return MacroBias.BEARISH
    return MacroBias.NEUTRAL


def _option_type_for(bias: MacroBias) -> OptionType:
    return OptionType.CALL if bias is MacroBias.BULLISH else OptionType.PUT


@register_strategy
class LongOptionStrategy:
    """Buy a single call on a bullish read or a single put on a bearish read."""

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

        stale = self._snapshot_is_stale(ctx)
        if stale is not None:
            return self._reject(decision, ctx, stale, "snapshot is stale or invalid")

        eligibility = self._option_eligibility_reason(ctx)
        if eligibility is not None:
            return self._reject(
                decision, ctx, eligibility, "option contract ineligible"
            )

        macro = ctx.macro
        macro_bias = accepted_macro_bias(
            macro, now=ctx.now, min_confidence=MACRO_MIN_CONFIDENCE
        )
        if macro_bias is not None and macro is not None:
            bias = macro_bias
            confidence = macro.confidence
        else:
            bias = _technical_bias(ctx)
            confidence = TECHNICAL_CONFIDENCE
        if bias is MacroBias.NEUTRAL:
            return decision  # no directional signal: no trade, not an error

        option_type = _option_type_for(bias)
        if ctx.option.contract.option_type is not option_type:
            return decision  # candidate does not match the read; skip, do not reject

        intent = self._build_intent(ctx, bias, option_type, confidence)
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
        if not ctx.option.permits_new_exposure:
            return ReasonCode.DATA_INVALID
        age = ctx.now - ctx.underlying.times.calculation_time
        if age > timedelta(seconds=MAX_SNAPSHOT_AGE_SECONDS):
            return ReasonCode.DATA_STALE
        if ctx.now < ctx.underlying.times.calculation_time:
            return ReasonCode.DATA_INVALID  # decision instant precedes the data
        return None

    def _option_eligibility_reason(self, ctx: StrategyContext) -> ReasonCode | None:
        option = ctx.option
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
        spread_fraction = (ask.value - bid.value) / mid
        if spread_fraction > MAX_ENTRY_SPREAD_FRACTION:
            return ReasonCode.SPREAD_TOO_WIDE
        return None

    def _build_intent(
        self,
        ctx: StrategyContext,
        bias: MacroBias,
        option_type: OptionType,
        confidence: Decimal,
    ) -> TradeIntent:
        risk = Money.of(REQUESTED_RISK_INR, _CURRENCY)
        setup_code = f"LONG_{option_type.value}_{bias.value}"
        now = ctx.now
        created_at = now
        expires_at = now + timedelta(seconds=INTENT_TTL_SECONDS)
        config_version = ctx.underlying.lineage.versions.config_version

        intent_id = _derive_id(
            self.strategy_id,
            self.strategy_version,
            ctx.underlying.snapshot_id,
            ctx.option.contract.symbol,
            setup_code,
            created_at.isoformat(),
        )

        return TradeIntent(
            intent_id=intent_id,
            correlation_id=_derive_id(self.strategy_id, ctx.underlying.snapshot_id),
            strategy_id=self.strategy_id,
            strategy_version=self.strategy_version,
            snapshot_id=ctx.underlying.snapshot_id,
            promoted_config_version=config_version,
            promoted_proposal_id=None,
            supersedes_intent_id=None,
            underlying=ctx.underlying.contract.underlying,
            asset_class=ctx.underlying.contract.asset_class,
            legs=(
                IntentLeg(
                    leg_id="leg-1",
                    contract=ctx.option.contract,
                    side=Side.BUY,
                    ratio=1,
                ),
            ),
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
            created_at=created_at,
            expires_at=expires_at,
        )


def _derive_id(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()
