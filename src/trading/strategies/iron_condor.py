"""Defined-risk four-leg Iron Condor strategy for neutral market regimes.

Combines a bull put credit spread and a bear call credit spread sharing one expiry
and identical wing widths:
  - Long Put  (buy lower wing strike)
  - Short Put (sell inner put strike)
  - Short Call (sell inner call strike)
  - Long Call (buy upper wing strike)

Emitted strictly when market regime is neutral and directional indicators express a
range-bound regime.
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

__all__ = ["STRATEGY_ID", "STRATEGY_VERSION", "IronCondorStrategy"]

STRATEGY_ID = "iron_condor"
STRATEGY_VERSION = "iron-condor-v1"

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
CONDOR_LEG_COUNT = 4
WING_LEG_COUNT = 2
PARTIAL_FILL_POLICY = "ALL_OR_CANCEL"
INVALIDATION_NOTE = (
    "Defined-risk four-leg iron condor; wing loss is capped at strike width."
)

_CURRENCY = Currency.INR


def _strike(option: FeatureSnapshot) -> Decimal:
    strike = option.contract.strike
    if strike is None:
        raise ValueError("option contract missing strike")
    return strike


def _derive_id(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


@register_strategy
class IronCondorStrategy:
    """Four-leg defined-risk neutral option strategy."""

    strategy_id: str = STRATEGY_ID
    strategy_version: str = STRATEGY_VERSION

    def evaluate(
        self,
        ctx: StrategyContext,
        *,
        put_wing: tuple[FeatureSnapshot, FeatureSnapshot] | None = None,
        call_wing: tuple[FeatureSnapshot, FeatureSnapshot] | None = None,
    ) -> StrategyDecision:
        """Evaluate market conditions and emit a 4-leg iron condor intent."""
        decision = StrategyDecision(
            strategy_id=self.strategy_id,
            strategy_version=self.strategy_version,
            snapshot_id=ctx.underlying.snapshot_id,
            as_of=ctx.now,
            intents=(),
            rejections=(),
        )

        # 1. Entry permission check
        if not ctx.view.entries_permitted:
            return self._reject(
                decision, ctx, ReasonCode.ENTRY_FROZEN, "entries not permitted"
            )

        # 2. Quote freshness check (calculation_time, not bar event_time)
        freshness = quote_freshness_for_context(ctx)
        if freshness.hard_reason is not None:
            return self._reject(
                decision,
                ctx,
                freshness.hard_reason,
                freshness.detail or "quote freshness check failed",
            )
        strict_would_block = freshness.strict_would_block

        # 3. Macro bias check (must be NEUTRAL)
        macro_bias = MacroBias.NEUTRAL
        confidence = Decimal("1.0")
        if ctx.macro is not None:
            macro_bias = ctx.macro.directional_bias
            confidence = ctx.macro.confidence

        if macro_bias is not MacroBias.NEUTRAL:
            return self._reject(
                decision,
                ctx,
                ReasonCode.DATA_INVALID,
                f"iron condor requires neutral regime, got {macro_bias.value}",
            )

        # 4. Resolve the four option candidates and validate wings
        res = self._resolve_wings(ctx, put_wing, call_wing)
        if isinstance(res[0], ReasonCode):
            return self._reject(decision, ctx, res[0], str(res[1]))

        long_put, short_put, short_call, long_call = res

        # 6. Construct 4 intent legs
        legs = (
            IntentLeg(
                leg_id="leg-long-put",
                ratio=1,
                side=Side.BUY,
                contract=long_put.contract,
            ),
            IntentLeg(
                leg_id="leg-short-put",
                ratio=1,
                side=Side.SELL,
                contract=short_put.contract,
            ),
            IntentLeg(
                leg_id="leg-short-call",
                ratio=1,
                side=Side.SELL,
                contract=short_call.contract,
            ),
            IntentLeg(
                leg_id="leg-long-call",
                ratio=1,
                side=Side.BUY,
                contract=long_call.contract,
            ),
        )

        risk = Money.of(REQUESTED_RISK_INR, _CURRENCY)
        setup_code = "IRON_CONDOR"
        now = ctx.now
        intent_id = _derive_id(
            self.strategy_id,
            self.strategy_version,
            ctx.underlying.snapshot_id,
            "|".join(leg.contract.symbol for leg in legs),
            setup_code,
            now.isoformat(),
        )

        intent = TradeIntent(
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
            mode_id=ModeId.M4_STRATEGIC_POSITIONAL,
            family_id=FamilyId.short_iron_condor_defined.value,
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

        return StrategyDecision(
            strategy_id=self.strategy_id,
            strategy_version=self.strategy_version,
            snapshot_id=ctx.underlying.snapshot_id,
            as_of=ctx.now,
            intents=(intent,),
            rejections=(),
            strict_would_block=strict_would_block,
        )

    @classmethod
    def _resolve_wings(
        cls,
        ctx: StrategyContext,
        put_wing: tuple[FeatureSnapshot, FeatureSnapshot] | None,
        call_wing: tuple[FeatureSnapshot, FeatureSnapshot] | None,
    ) -> (
        tuple[ReasonCode, str]
        | tuple[FeatureSnapshot, FeatureSnapshot, FeatureSnapshot, FeatureSnapshot]
    ):
        if put_wing is not None and call_wing is not None:
            long_put, short_put = put_wing
            short_call, long_call = call_wing
        elif len(ctx.candidates) == CONDOR_LEG_COUNT:
            puts = [
                c for c in ctx.candidates if c.contract.option_type is OptionType.PUT
            ]
            calls = [
                c for c in ctx.candidates if c.contract.option_type is OptionType.CALL
            ]
            if len(puts) != WING_LEG_COUNT or len(calls) != WING_LEG_COUNT:
                return (
                    ReasonCode.INSTRUMENT_UNKNOWN,
                    "iron condor requires two calls and two puts",
                )
            sorted_puts = sorted(puts, key=_strike)
            long_put, short_put = sorted_puts[0], sorted_puts[1]
            sorted_calls = sorted(calls, key=_strike)
            short_call, long_call = sorted_calls[0], sorted_calls[1]
        else:
            return (
                ReasonCode.INSTRUMENT_UNKNOWN,
                f"iron condor requires {CONDOR_LEG_COUNT} option candidates",
            )

        lp_strike = _strike(long_put)
        sp_strike = _strike(short_put)
        sc_strike = _strike(short_call)
        lc_strike = _strike(long_call)

        if not (lp_strike < sp_strike < sc_strike < lc_strike):
            msg = (
                f"invalid strike ordering: "
                f"{lp_strike} < {sp_strike} < {sc_strike} < {lc_strike}"
            )
            return (ReasonCode.DATA_INVALID, msg)

        put_width = sp_strike - lp_strike
        call_width = lc_strike - sc_strike
        if put_width != call_width:
            return (
                ReasonCode.DATA_INVALID,
                f"unequal wing widths: put {put_width} != call {call_width}",
            )

        return (long_put, short_put, short_call, long_call)

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
            rejections=(rejection,),
        )
