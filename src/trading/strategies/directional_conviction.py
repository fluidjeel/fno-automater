"""Directional conviction strategy plugin.

Follows the pure strategy pattern, sizing options based on directional conviction.
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

__all__ = ["DirectionalConvictionStrategy"]

STRATEGY_ID = "directional_conviction"
STRATEGY_VERSION = "directional-conviction-v1"
REQUESTED_RISK_INR = Decimal("10000")
MIN_DAYS_TO_EXPIRY = 10
MIN_OPEN_INTEREST = 1000
MAX_SNAPSHOT_AGE_SECONDS = 120
MAX_ENTRY_SPREAD_FRACTION = Decimal("0.05")
STOP_TICKS = 30
TARGET_TICKS = 60
EXIT_BEFORE_EXPIRY_DAYS = 2
ENTRY_TIMEOUT_SECONDS = 30
INTENT_TTL_SECONDS = 300
SESSION_LABEL = "NSE_FO"
INVALIDATION_NOTE = "Directional conviction premium position"
MIN_CONVICTION_SCORE = Decimal("50")  # Minimum required conviction to enter

_CURRENCY = Currency.INR


@register_strategy
class DirectionalConvictionStrategy:
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
        if not ctx.candidates:
            return self._reject(
                decision, ctx, ReasonCode.INSTRUMENT_UNKNOWN, "no candidates provided"
            )

        reason = self._validation_reason(ctx)
        if reason is not None:
            return self._reject(decision, ctx, reason, "input failed validation")

        conviction_score_val = ctx.underlying.features.get("conviction_score")
        conviction_dir_val = ctx.underlying.features.get("conviction_direction")

        if conviction_score_val is None or conviction_dir_val is None:
            return decision

        if conviction_score_val < MIN_CONVICTION_SCORE:
            return decision

        if conviction_dir_val == 1:
            option_type = OptionType.CALL
            setup_code = "CONVICTION_LONG"
        elif conviction_dir_val == -1:
            option_type = OptionType.PUT
            setup_code = "CONVICTION_SHORT"
        else:
            return decision

        matching_option = None
        for cand in ctx.candidates:
            if (
                cand.contract.option_type is option_type
                and self._option_eligibility_reason(cand) is None
            ):
                matching_option = cand
                break

        if not matching_option:
            return decision

        intent = self._build_intent(
            ctx, matching_option, setup_code, option_type, conviction_score_val
        )
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
        age = ctx.now - ctx.underlying.times.calculation_time
        if age > timedelta(seconds=MAX_SNAPSHOT_AGE_SECONDS):
            return ReasonCode.DATA_STALE
        if ctx.now < ctx.underlying.times.calculation_time:
            return ReasonCode.DATA_INVALID
        return None

    def _option_eligibility_reason(self, option: FeatureSnapshot) -> ReasonCode | None:
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
        mid = (bid.value + ask.value) / Decimal("2")
        if mid <= 0:
            return ReasonCode.PRICE_UNAVAILABLE
        spread_fraction = (ask.value - bid.value) / mid
        if spread_fraction > MAX_ENTRY_SPREAD_FRACTION:
            return ReasonCode.SPREAD_TOO_WIDE
        return None

    def _build_intent(
        self,
        ctx: StrategyContext,
        option: FeatureSnapshot,
        setup_code: str,
        option_type: OptionType,
        conviction_score: Decimal,
    ) -> TradeIntent:
        risk = Money.of(REQUESTED_RISK_INR, _CURRENCY)
        now = ctx.now
        config_version = ctx.underlying.lineage.versions.config_version

        intent_id = _derive_id(
            self.strategy_id,
            self.strategy_version,
            ctx.underlying.snapshot_id,
            option.contract.symbol,
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
            promoted_config_version=config_version,
            promoted_proposal_id=None,
            supersedes_intent_id=None,
            underlying=ctx.underlying.contract.underlying,
            asset_class=ctx.underlying.contract.asset_class,
            legs=(
                IntentLeg(
                    leg_id="leg-1",
                    contract=option.contract,
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
            strategy_confidence=conviction_score / Decimal("100"),
            created_at=now,
            expires_at=now + timedelta(seconds=INTENT_TTL_SECONDS),
        )


def _derive_id(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()
