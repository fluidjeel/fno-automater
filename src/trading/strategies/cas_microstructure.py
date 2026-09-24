"""Close-auction (CAS) microstructure — Layer 3 slice 4.

A single long option leg, timed by documented close-auction microstructure
features rather than by a positional technical read. The features are read from
``FeatureSnapshot.features`` under the exact keys this module declares for
``FEATURE_SET_VERSION``. A snapshot that omits any of them is rejected: a
microstructure edge that was never measured cannot be assumed, and defaulting it
would fabricate a signal.

Two deterministic guards keep the horizon honest:

  - The decision is only valid inside the closing window derived from the
    injected ``now``. No wall clock is read, so a replay of the same instant
    makes the same call.
  - The intent carries a mandatory short time exit, so the position cannot
    outlive the auction effect it was built for.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal

from trading.domain.contracts import (
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

__all__ = ["CasMicrostructureStrategy"]

STRATEGY_ID = "cas_microstructure"
STRATEGY_VERSION = "cas-microstructure-v1"

# The versioned microstructure feature contract. These keys are the only feature
# inputs this strategy understands; adding, renaming or redefining any of them
# changes STRATEGY_VERSION. They are read from the underlying snapshot.
FEATURE_SET_VERSION = "cas-microstructure-v1"
FEATURE_AUCTION_IMBALANCE = "cas_auction_imbalance"
FEATURE_TRADE_FLOW_IMBALANCE = "cas_trade_flow_imbalance"
FEATURE_MICROPRICE_EDGE_BPS = "cas_microprice_edge_bps"
FEATURE_QUOTE_INSTABILITY = "cas_quote_instability"
REQUIRED_FEATURES = (
    FEATURE_AUCTION_IMBALANCE,
    FEATURE_TRADE_FLOW_IMBALANCE,
    FEATURE_MICROPRICE_EDGE_BPS,
    FEATURE_QUOTE_INSTABILITY,
)

# Versioned research parameters. These are uncalibrated and must not be treated
# as validated risk policy.
REQUESTED_RISK_INR = Decimal("10000")
MIN_DAYS_TO_EXPIRY = 1
MIN_OPEN_INTEREST = 500
MAX_SNAPSHOT_AGE_SECONDS = 30
MAX_ENTRY_SPREAD_FRACTION = Decimal("0.05")
MIN_NET_PRESSURE = Decimal("0.10")
MAX_QUOTE_INSTABILITY = Decimal("0.50")
STOP_TICKS = 20
TARGET_TICKS = 30
EXIT_BEFORE_EXPIRY_DAYS = 1
CAS_HOLDING_SECONDS = 900
MAX_HOLDING_DAYS = 1
ENTRY_TIMEOUT_SECONDS = 15
INTENT_TTL_SECONDS = 60
SESSION_LABEL = "NSE_FO"
INVALIDATION_NOTE = (
    "Close-auction microstructure position; the time exit ends the auction effect."
)

# Decision windows match CasEventDrivenConfig scan windows (ops): continuous
# 09:20–15:00 and closing context 15:00–15:25. Cash auction 15:30–15:40 is out.
IST = timezone(timedelta(hours=5, minutes=30))
CAS_WINDOW_START_IST = time(9, 20)
CAS_WINDOW_END_IST = time(15, 25)
CAS_TIME_EXIT_DEADLINE_IST = time(15, 25)
SATURDAY_WEEKDAY = 5

_CURRENCY = Currency.INR


def _m1_time_exit(now: datetime) -> datetime:
    """Hold up to CAS_HOLDING_SECONDS, never past the 15:25 IST closing cutoff."""
    local = now.astimezone(IST)
    deadline_local = datetime.combine(
        local.date(), CAS_TIME_EXIT_DEADLINE_IST, tzinfo=IST
    )
    candidate = now + timedelta(seconds=CAS_HOLDING_SECONDS)
    deadline_utc = deadline_local.astimezone(now.tzinfo) if now.tzinfo else deadline_local
    return min(candidate, deadline_utc)


def _option_type_for(bias: MacroBias) -> OptionType:
    return OptionType.CALL if bias is MacroBias.BULLISH else OptionType.PUT


@register_strategy
class CasMicrostructureStrategy:
    """Buy a call on auction-supported strength, a put on auction-supported weakness."""

    strategy_id = STRATEGY_ID
    strategy_version = STRATEGY_VERSION

    def __init__(
        self,
        *,
        stop_ticks: int = STOP_TICKS,
        target_ticks: int | None = TARGET_TICKS,
        trailing_activation_ticks: int | None = None,
        trailing_distance_ticks: int | None = None,
    ) -> None:
        self._stop_ticks = stop_ticks
        self._target_ticks = target_ticks
        self._trailing_activation_ticks = trailing_activation_ticks
        self._trailing_distance_ticks = trailing_distance_ticks

    @property
    def stop_ticks(self) -> int:
        return self._stop_ticks

    @property
    def target_ticks(self) -> int | None:
        return self._target_ticks

    @property
    def trailing_activation_ticks(self) -> int | None:
        return self._trailing_activation_ticks

    @property
    def trailing_distance_ticks(self) -> int | None:
        return self._trailing_distance_ticks

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
                "exactly one option required",
            )

        reason = self._validation_reason(ctx)
        if reason is not None:
            detail = f"input failed validation for feature set {FEATURE_SET_VERSION}"
            return self._reject(decision, ctx, reason, detail)

        bias, confidence = resolve_direction(
            ctx.underlying,
            ctx.macro,
            ctx.now,
            min_confidence=DEFAULT_MACRO_MIN_CONFIDENCE,
        )
        if bias is MacroBias.NEUTRAL:
            return decision  # no directional signal: no trade, not an error

        option = ctx.candidates[0]
        option_type = _option_type_for(bias)
        mismatched = (
            option.contract.option_type is not option_type
            or not self._microstructure_confirms(ctx.underlying, bias)
        )
        if mismatched:
            return decision  # candidate or measured pressure: no trade, not an error

        intent = self._build_intent(ctx, option, bias, option_type, confidence)
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
        option = ctx.candidates[0]
        if not ctx.underlying.permits_new_exposure or not option.permits_new_exposure:
            return ReasonCode.DATA_INVALID
        window = self._window_reason(ctx)
        if window is not None:
            return window
        feature = self._feature_reason(ctx.underlying)
        if feature is not None:
            return feature
        return self._option_eligibility_reason(option)

    @staticmethod
    def _window_reason(ctx: StrategyContext) -> ReasonCode | None:
        """Session window first, then freshness — both derived from ``ctx.now``."""
        local = ctx.now.astimezone(IST)
        if local.weekday() >= SATURDAY_WEEKDAY:
            return ReasonCode.OUTSIDE_SESSION
        # Half-open end matches event-path scan windows (end exclusive at 15:25).
        if not (CAS_WINDOW_START_IST <= local.time() < CAS_WINDOW_END_IST):
            return ReasonCode.OUTSIDE_SESSION
        calculation_time = ctx.underlying.times.calculation_time
        if ctx.now < calculation_time:
            return ReasonCode.DATA_INVALID  # decision instant precedes the data
        if ctx.now - calculation_time > timedelta(seconds=MAX_SNAPSHOT_AGE_SECONDS):
            return ReasonCode.DATA_STALE
        return None

    @staticmethod
    def _feature_reason(underlying: FeatureSnapshot) -> ReasonCode | None:
        """A declared feature that is absent is a gap, never a default."""
        if underlying.feature_set_version != FEATURE_SET_VERSION:
            return ReasonCode.DATA_INVALID
        if any(key not in underlying.features for key in REQUIRED_FEATURES):
            return ReasonCode.DATA_GAP
        if underlying.features[FEATURE_QUOTE_INSTABILITY] > MAX_QUOTE_INSTABILITY:
            return ReasonCode.DATA_DEGRADED
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
        mid = (bid.value + ask.value) / 2
        if mid <= 0:
            return ReasonCode.PRICE_UNAVAILABLE
        if (ask.value - bid.value) / mid > MAX_ENTRY_SPREAD_FRACTION:
            return ReasonCode.SPREAD_TOO_WIDE
        return None

    @staticmethod
    def _microstructure_confirms(underlying: FeatureSnapshot, bias: MacroBias) -> bool:
        """The measured auction pressure and microprice edge must point the same way.

        Feature presence was already established by ``_feature_reason``, so this
        reads the declared keys directly rather than re-deriving a default.
        """
        features = underlying.features
        pressure = (
            features[FEATURE_AUCTION_IMBALANCE] + features[FEATURE_TRADE_FLOW_IMBALANCE]
        )
        edge = features[FEATURE_MICROPRICE_EDGE_BPS]
        if bias is MacroBias.BULLISH:
            return pressure >= MIN_NET_PRESSURE and edge >= 0
        return pressure <= -MIN_NET_PRESSURE and edge <= 0

    def _build_intent(
        self,
        ctx: StrategyContext,
        option: FeatureSnapshot,
        bias: MacroBias,
        option_type: OptionType,
        confidence: Decimal,
    ) -> TradeIntent:
        risk = Money.of(REQUESTED_RISK_INR, _CURRENCY)
        setup_code = f"CAS_{option_type.value}_{bias.value}"
        now = ctx.now
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
            promoted_config_version=ctx.underlying.lineage.versions.config_version,
            promoted_proposal_id=None,
            supersedes_intent_id=None,
            mode_id=ModeId.M1_CAS,
            family_id=(
                FamilyId.long_call.value
                if option_type is OptionType.CALL
                else FamilyId.long_put.value
            ),
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
                stop_distance_ticks=self._stop_ticks,
                target_distance_ticks=self._target_ticks,
                break_even_trigger_ticks=None,
                trailing_activation_ticks=self._trailing_activation_ticks,
                trailing_distance_ticks=self._trailing_distance_ticks,
                time_exit=_m1_time_exit(now),
                exit_before_expiry_days=EXIT_BEFORE_EXPIRY_DAYS,
                invalidation_note=INVALIDATION_NOTE,
                partial_fill_policy="CANCEL_REMAINDER",
            ),
            constraints=IntentConstraints(
                min_days_to_expiry=MIN_DAYS_TO_EXPIRY,
                max_holding_days=MAX_HOLDING_DAYS,
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
