"""M4 broad basket strategies: butterflies, iron butterfly, straddle, strangle."""

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
from trading.domain.enums import FamilyId, ModeId, OptionType, ReasonCode, Side
from trading.domain.primitives import Currency, Money, Percent
from trading.strategies.base import (
    Rejection,
    StrategyContext,
    StrategyDecision,
    register_strategy,
)
from trading.strategies.macro import MacroBias
from trading.strategies.quote_freshness import quote_freshness_for_context

__all__ = [
    "LongCallButterflyStrategy",
    "LongPutButterflyStrategy",
    "LongStraddleStrategy",
    "LongStrangleStrategy",
    "ShortIronButterflyStrategy",
]

REQUESTED_RISK_INR = Decimal("10000")
MIN_DAYS_TO_EXPIRY = 7
MIN_OPEN_INTEREST = 1000
MAX_ENTRY_SPREAD_FRACTION = Decimal("0.05")
STOP_TICKS = 40
TARGET_TICKS = 40
EXIT_BEFORE_EXPIRY_DAYS = 1
ENTRY_TIMEOUT_SECONDS = 30
INTENT_TTL_SECONDS = 300
SESSION_LABEL = "NSE_FO"
PARTIAL_FILL_POLICY = "ALL_OR_CANCEL"
_CURRENCY = Currency.INR
_MIDDLE_RATIO = 2


def _strike(option: FeatureSnapshot) -> Decimal:
    strike = option.contract.strike
    if strike is None:
        raise ValueError("option contract missing strike")
    return strike


def _derive_id(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def _base_reject(
    decision: StrategyDecision,
    ctx: StrategyContext,
    code: ReasonCode,
    detail: str,
) -> StrategyDecision:
    return StrategyDecision(
        strategy_id=decision.strategy_id,
        strategy_version=decision.strategy_version,
        snapshot_id=decision.snapshot_id,
        as_of=decision.as_of,
        intents=(),
        rejections=(
            *decision.rejections,
            Rejection(
                reason=code,
                detail=detail,
                snapshot_id=ctx.underlying.snapshot_id,
                as_of=ctx.now,
            ),
        ),
    )


def _common_checks(
    ctx: StrategyContext, decision: StrategyDecision
) -> tuple[StrategyDecision | None, tuple[ReasonCode, ...]]:
    if not ctx.view.entries_permitted:
        return (
            _base_reject(
                decision, ctx, ReasonCode.ENTRY_FROZEN, "entries not permitted"
            ),
            (),
        )
    freshness = quote_freshness_for_context(ctx)
    if freshness.hard_reason is not None:
        return (
            _base_reject(
                decision,
                ctx,
                freshness.hard_reason,
                freshness.detail or "quote freshness check failed",
            ),
            (),
        )
    return None, freshness.strict_would_block


def _build_intent(
    *,
    ctx: StrategyContext,
    strategy_id: str,
    strategy_version: str,
    family_id: str,
    setup_code: str,
    legs: tuple[IntentLeg, ...],
    invalidation_note: str,
    confidence: Decimal,
) -> TradeIntent:
    risk = Money.of(REQUESTED_RISK_INR, _CURRENCY)
    now = ctx.now
    intent_id = _derive_id(
        strategy_id,
        strategy_version,
        ctx.underlying.snapshot_id,
        "|".join(leg.contract.symbol for leg in legs),
        setup_code,
        now.isoformat(),
    )
    return TradeIntent(
        intent_id=intent_id,
        correlation_id=_derive_id(strategy_id, ctx.underlying.snapshot_id),
        strategy_id=strategy_id,
        strategy_version=strategy_version,
        snapshot_id=ctx.underlying.snapshot_id,
        experiment_id=ctx.experiment_id,
        execution_mode=ctx.execution_mode,
        promoted_config_version=ctx.underlying.lineage.versions.config_version,
        promoted_proposal_id=None,
        supersedes_intent_id=None,
        mode_id=ModeId.M4_STRATEGIC_POSITIONAL,
        family_id=family_id,
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
            invalidation_note=invalidation_note,
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


def _resolve_butterfly(
    ctx: StrategyContext,
    *,
    option_type: OptionType,
) -> tuple[FeatureSnapshot, FeatureSnapshot, FeatureSnapshot] | ReasonCode:
    options = [
        item for item in ctx.candidates if item.contract.option_type is option_type
    ]
    if len(options) < 3:
        return ReasonCode.INSTRUMENT_UNKNOWN
    combos: list[tuple[Decimal, FeatureSnapshot, FeatureSnapshot, FeatureSnapshot]] = []
    for i in range(len(options)):
        for j in range(i + 1, len(options)):
            for k in range(j + 1, len(options)):
                low, mid, high = sorted(
                    (options[i], options[j], options[k]), key=_strike
                )
                low_strike = _strike(low)
                mid_strike = _strike(mid)
                high_strike = _strike(high)
                if high_strike - mid_strike != mid_strike - low_strike:
                    continue
                score = Decimal("1")
                combos.append((score, low, mid, high))
    if not combos:
        return ReasonCode.INSTRUMENT_UNKNOWN
    combos.sort(key=lambda row: (-row[0], row[1].contract.symbol))
    _, low, mid, high = combos[0]
    return low, mid, high


@register_strategy
class LongCallButterflyStrategy:
    """Long 1:2:1 call butterfly for range-bound M4."""

    strategy_id = FamilyId.long_call_butterfly.value
    strategy_version = "long-call-butterfly-v1"

    def evaluate(self, ctx: StrategyContext) -> StrategyDecision:
        decision = StrategyDecision(
            strategy_id=self.strategy_id,
            strategy_version=self.strategy_version,
            snapshot_id=ctx.underlying.snapshot_id,
            as_of=ctx.now,
            intents=(),
            rejections=(),
        )
        blocked, strict_would_block = _common_checks(ctx, decision)
        if blocked is not None:
            return blocked
        resolved = _resolve_butterfly(ctx, option_type=OptionType.CALL)
        if isinstance(resolved, ReasonCode):
            return _base_reject(decision, ctx, resolved, "no symmetric call butterfly")
        low, mid, high = resolved
        legs = (
            IntentLeg(
                leg_id="leg-low-wing", contract=low.contract, side=Side.BUY, ratio=1
            ),
            IntentLeg(
                leg_id="leg-short-body",
                contract=mid.contract,
                side=Side.SELL,
                ratio=_MIDDLE_RATIO,
            ),
            IntentLeg(
                leg_id="leg-high-wing", contract=high.contract, side=Side.BUY, ratio=1
            ),
        )
        intent = _build_intent(
            ctx=ctx,
            strategy_id=self.strategy_id,
            strategy_version=self.strategy_version,
            family_id=FamilyId.long_call_butterfly.value,
            setup_code="LONG_CALL_BUTTERFLY",
            legs=legs,
            invalidation_note="Defined-risk long call butterfly; maximum loss is the net debit.",
            confidence=Decimal("0.8"),
        )
        return StrategyDecision(
            strategy_id=self.strategy_id,
            strategy_version=self.strategy_version,
            snapshot_id=ctx.underlying.snapshot_id,
            as_of=ctx.now,
            intents=(intent,),
            rejections=(),
            strict_would_block=strict_would_block,
        )


@register_strategy
class LongPutButterflyStrategy:
    """Long 1:2:1 put butterfly for range-bound M4."""

    strategy_id = FamilyId.long_put_butterfly.value
    strategy_version = "long-put-butterfly-v1"

    def evaluate(self, ctx: StrategyContext) -> StrategyDecision:
        decision = StrategyDecision(
            strategy_id=self.strategy_id,
            strategy_version=self.strategy_version,
            snapshot_id=ctx.underlying.snapshot_id,
            as_of=ctx.now,
            intents=(),
            rejections=(),
        )
        blocked, strict_would_block = _common_checks(ctx, decision)
        if blocked is not None:
            return blocked
        resolved = _resolve_butterfly(ctx, option_type=OptionType.PUT)
        if isinstance(resolved, ReasonCode):
            return _base_reject(decision, ctx, resolved, "no symmetric put butterfly")
        low, mid, high = resolved
        legs = (
            IntentLeg(
                leg_id="leg-low-wing", contract=low.contract, side=Side.BUY, ratio=1
            ),
            IntentLeg(
                leg_id="leg-short-body",
                contract=mid.contract,
                side=Side.SELL,
                ratio=_MIDDLE_RATIO,
            ),
            IntentLeg(
                leg_id="leg-high-wing", contract=high.contract, side=Side.BUY, ratio=1
            ),
        )
        intent = _build_intent(
            ctx=ctx,
            strategy_id=self.strategy_id,
            strategy_version=self.strategy_version,
            family_id=FamilyId.long_put_butterfly.value,
            setup_code="LONG_PUT_BUTTERFLY",
            legs=legs,
            invalidation_note="Defined-risk long put butterfly; maximum loss is the net debit.",
            confidence=Decimal("0.8"),
        )
        return StrategyDecision(
            strategy_id=self.strategy_id,
            strategy_version=self.strategy_version,
            snapshot_id=ctx.underlying.snapshot_id,
            as_of=ctx.now,
            intents=(intent,),
            rejections=(),
            strict_would_block=strict_would_block,
        )


@register_strategy
class ShortIronButterflyStrategy:
    """Short iron butterfly for range-bound M4."""

    strategy_id = FamilyId.short_iron_butterfly_defined.value
    strategy_version = "short-iron-butterfly-v1"

    def evaluate(self, ctx: StrategyContext) -> StrategyDecision:
        decision = StrategyDecision(
            strategy_id=self.strategy_id,
            strategy_version=self.strategy_version,
            snapshot_id=ctx.underlying.snapshot_id,
            as_of=ctx.now,
            intents=(),
            rejections=(),
        )
        blocked, strict_would_block = _common_checks(ctx, decision)
        if blocked is not None:
            return blocked
        if (
            ctx.macro is not None
            and ctx.macro.directional_bias is not MacroBias.NEUTRAL
        ):
            return _base_reject(
                decision,
                ctx,
                ReasonCode.DATA_INVALID,
                "iron butterfly requires neutral regime",
            )
        if len(ctx.candidates) != 4:
            return _base_reject(
                decision,
                ctx,
                ReasonCode.INSTRUMENT_UNKNOWN,
                "exactly four options required",
            )
        puts = [
            item
            for item in ctx.candidates
            if item.contract.option_type is OptionType.PUT
        ]
        calls = [
            item
            for item in ctx.candidates
            if item.contract.option_type is OptionType.CALL
        ]
        if len(puts) != 2 or len(calls) != 2:
            return _base_reject(
                decision, ctx, ReasonCode.INSTRUMENT_UNKNOWN, "invalid wing count"
            )
        long_put = min(puts, key=_strike)
        short_put = max(puts, key=_strike)
        short_call = min(calls, key=_strike)
        long_call = max(calls, key=_strike)
        if _strike(short_put) != _strike(short_call):
            return _base_reject(
                decision,
                ctx,
                ReasonCode.INSTRUMENT_UNKNOWN,
                "short legs must share center strike",
            )
        if not (_strike(long_put) < _strike(short_put) < _strike(long_call)):
            return _base_reject(
                decision, ctx, ReasonCode.INSTRUMENT_UNKNOWN, "invalid strike order"
            )
        legs = (
            IntentLeg(
                leg_id="leg-long-put",
                contract=long_put.contract,
                side=Side.BUY,
                ratio=1,
            ),
            IntentLeg(
                leg_id="leg-long-call",
                contract=long_call.contract,
                side=Side.BUY,
                ratio=1,
            ),
            IntentLeg(
                leg_id="leg-short-put",
                contract=short_put.contract,
                side=Side.SELL,
                ratio=1,
            ),
            IntentLeg(
                leg_id="leg-short-call",
                contract=short_call.contract,
                side=Side.SELL,
                ratio=1,
            ),
        )
        intent = _build_intent(
            ctx=ctx,
            strategy_id=self.strategy_id,
            strategy_version=self.strategy_version,
            family_id=FamilyId.short_iron_butterfly_defined.value,
            setup_code="SHORT_IRON_BUTTERFLY",
            legs=legs,
            invalidation_note="Defined-risk short iron butterfly; wing loss is capped.",
            confidence=Decimal("0.8"),
        )
        return StrategyDecision(
            strategy_id=self.strategy_id,
            strategy_version=self.strategy_version,
            snapshot_id=ctx.underlying.snapshot_id,
            as_of=ctx.now,
            intents=(intent,),
            rejections=(),
            strict_would_block=strict_would_block,
        )


@register_strategy
class LongStraddleStrategy:
    """Debit-only long straddle for expanding-volatility M4."""

    strategy_id = FamilyId.long_straddle.value
    strategy_version = "long-straddle-v1"

    def evaluate(self, ctx: StrategyContext) -> StrategyDecision:
        decision = StrategyDecision(
            strategy_id=self.strategy_id,
            strategy_version=self.strategy_version,
            snapshot_id=ctx.underlying.snapshot_id,
            as_of=ctx.now,
            intents=(),
            rejections=(),
        )
        blocked, strict_would_block = _common_checks(ctx, decision)
        if blocked is not None:
            return blocked
        if len(ctx.candidates) != 2:
            return _base_reject(
                decision,
                ctx,
                ReasonCode.INSTRUMENT_UNKNOWN,
                "exactly two options required",
            )
        call = next(
            (
                item
                for item in ctx.candidates
                if item.contract.option_type is OptionType.CALL
            ),
            None,
        )
        put = next(
            (
                item
                for item in ctx.candidates
                if item.contract.option_type is OptionType.PUT
            ),
            None,
        )
        if call is None or put is None:
            return _base_reject(
                decision, ctx, ReasonCode.INSTRUMENT_UNKNOWN, "missing call or put"
            )
        if _strike(call) != _strike(put):
            return _base_reject(
                decision, ctx, ReasonCode.INSTRUMENT_UNKNOWN, "strikes must match"
            )
        legs = (
            IntentLeg(
                leg_id="leg-call", contract=call.contract, side=Side.BUY, ratio=1
            ),
            IntentLeg(leg_id="leg-put", contract=put.contract, side=Side.BUY, ratio=1),
        )
        intent = _build_intent(
            ctx=ctx,
            strategy_id=self.strategy_id,
            strategy_version=self.strategy_version,
            family_id=FamilyId.long_straddle.value,
            setup_code="LONG_STRADDLE",
            legs=legs,
            invalidation_note="Long straddle; maximum loss is total debit.",
            confidence=Decimal("0.75"),
        )
        return StrategyDecision(
            strategy_id=self.strategy_id,
            strategy_version=self.strategy_version,
            snapshot_id=ctx.underlying.snapshot_id,
            as_of=ctx.now,
            intents=(intent,),
            rejections=(),
            strict_would_block=strict_would_block,
        )


@register_strategy
class LongStrangleStrategy:
    """Debit-only long strangle for expanding-volatility M4."""

    strategy_id = FamilyId.long_strangle.value
    strategy_version = "long-strangle-v1"

    def evaluate(self, ctx: StrategyContext) -> StrategyDecision:
        decision = StrategyDecision(
            strategy_id=self.strategy_id,
            strategy_version=self.strategy_version,
            snapshot_id=ctx.underlying.snapshot_id,
            as_of=ctx.now,
            intents=(),
            rejections=(),
        )
        blocked, strict_would_block = _common_checks(ctx, decision)
        if blocked is not None:
            return blocked
        if len(ctx.candidates) != 2:
            return _base_reject(
                decision,
                ctx,
                ReasonCode.INSTRUMENT_UNKNOWN,
                "exactly two options required",
            )
        call = next(
            (
                item
                for item in ctx.candidates
                if item.contract.option_type is OptionType.CALL
            ),
            None,
        )
        put = next(
            (
                item
                for item in ctx.candidates
                if item.contract.option_type is OptionType.PUT
            ),
            None,
        )
        if call is None or put is None:
            return _base_reject(
                decision, ctx, ReasonCode.INSTRUMENT_UNKNOWN, "missing call or put"
            )
        if _strike(put) >= _strike(call):
            return _base_reject(
                decision,
                ctx,
                ReasonCode.INSTRUMENT_UNKNOWN,
                "put strike must be below call strike",
            )
        legs = (
            IntentLeg(leg_id="leg-put", contract=put.contract, side=Side.BUY, ratio=1),
            IntentLeg(
                leg_id="leg-call", contract=call.contract, side=Side.BUY, ratio=1
            ),
        )
        intent = _build_intent(
            ctx=ctx,
            strategy_id=self.strategy_id,
            strategy_version=self.strategy_version,
            family_id=FamilyId.long_strangle.value,
            setup_code="LONG_STRANGLE",
            legs=legs,
            invalidation_note="Long strangle; maximum loss is total debit.",
            confidence=Decimal("0.75"),
        )
        return StrategyDecision(
            strategy_id=self.strategy_id,
            strategy_version=self.strategy_version,
            snapshot_id=ctx.underlying.snapshot_id,
            as_of=ctx.now,
            intents=(intent,),
            rejections=(),
            strict_would_block=strict_would_block,
        )
