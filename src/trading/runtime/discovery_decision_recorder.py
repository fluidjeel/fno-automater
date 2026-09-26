"""Build and persist DISCOVERY_DECISION records for evaluations and exits."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from trading.config.discovery import DiscoveryConfig
from trading.domain.contracts import TradeIntent
from trading.domain.contracts.discovery_decision import (
    DiscoveryCandidateRow,
    DiscoveryDecision,
    DiscoveryDecisionInputs,
    DiscoveryFillSnapshot,
    DiscoverySizingSnapshot,
)
from trading.domain.contracts.snapshot import FeatureSnapshot
from trading.domain.enums import (
    DiscoveryDecisionKind,
    DiscoveryStage,
    EntryProfile,
    ModeId,
    ReasonCode,
    RiskAction,
)
from trading.domain.ids import IdFactory
from trading.domain.primitives import Money, Price
from trading.risk.gate_profile import is_soft
from trading.runtime.decision_text import DecisionTextContext, render_decision_text
from trading.storage.trading_store import TradingEventType, TradingStore
from trading.trade.exits import ExitKind

if TYPE_CHECKING:
    from trading.domain.contracts import MarketState
    from trading.domain.contracts.position import PositionState
    from trading.runtime.paper_runner import (
        PaperCycleResult,
        PaperStrategyOutcome,
        PaperStrategyRequest,
    )

__all__ = [
    "build_decision_record",
    "persist_cycle_decisions",
    "persist_data_feed_error",
    "persist_exit_decision",
    "query_decisions",
]

_DIRECTION_CODES = frozenset(
    {
        ReasonCode.DIRECTION_NEUTRAL,
        ReasonCode.DIRECTION_UNRESOLVED,
        ReasonCode.DIRECTION_FALLBACK,
        ReasonCode.OPTION_TYPE_MISMATCH,
        ReasonCode.MICROSTRUCTURE_UNCONFIRMED,
        ReasonCode.CONVICTION_BELOW_THRESHOLD,
        ReasonCode.REGIME_NOT_RANGE,
        ReasonCode.VOL_COMPRESSED,
    }
)
_HARD_BLOCK_CODES = frozenset(
    {
        ReasonCode.ENTRY_FROZEN,
        ReasonCode.DATA_STALE,
        ReasonCode.DATA_INVALID,
        ReasonCode.PRICE_UNAVAILABLE,
        ReasonCode.KILL_SWITCH_ACTIVE,
        ReasonCode.PROTECTION_DEGRADED,
        ReasonCode.SYSTEM_NOT_READY,
        ReasonCode.RECONCILIATION_UNRESOLVED,
        ReasonCode.RISK_LIMIT_TRADE,
        ReasonCode.DATA_FEED_ERROR,
    }
)
_NEUTRAL_THRESHOLD = Decimal("0.30")


def persist_cycle_decisions(
    store: TradingStore,
    id_factory: IdFactory,
    result: PaperCycleResult,
    requests: tuple[PaperStrategyRequest, ...],
    *,
    cycle_id: str,
    as_of: datetime,
    profile_version: str,
    code_version: str,
    entry_profile: EntryProfile,
    discovery_config: DiscoveryConfig | None,
    market_state: MarketState | None = None,
) -> tuple[int, ...]:
    """Append one DISCOVERY_DECISION per evaluated (mode, family) pair."""
    sequences: list[int] = []
    for request, outcome in zip(requests, result.outcomes, strict=True):
        record = build_decision_record(
            request,
            outcome,
            cycle_id=cycle_id,
            as_of=as_of,
            profile_version=profile_version,
            code_version=code_version,
            entry_profile=entry_profile,
            discovery_config=discovery_config,
            market_state=market_state,
            id_factory=id_factory,
            arbitration_result=result.arbitration_result,
        )
        sequences.append(
            store.append(
                TradingEventType.DISCOVERY_DECISION,
                record,
                event_id=record.decision_id,
                recorded_at=as_of,
            )
        )
    return tuple(sequences)


def persist_data_feed_error(
    store: TradingStore,
    id_factory: IdFactory,
    *,
    cycle_id: str,
    as_of: datetime,
    profile_version: str,
    code_version: str,
    detail: str,
    experiment_id: str,
    family_id: str = "DATA_FEED",
) -> int:
    """Append one DATA-stage BLOCKED_HARD record for a session feed failure."""
    ctx = DecisionTextContext(detail=detail)
    reason_text = render_decision_text(
        decision=DiscoveryDecisionKind.BLOCKED_HARD,
        reason_codes=(ReasonCode.DATA_FEED_ERROR,),
        ctx=ctx,
    )
    record = DiscoveryDecision(
        decision_id=id_factory.new_id("DDEC"),
        cycle_id=cycle_id,
        as_of=as_of,
        mode_id=ModeId.M2_DIRECTIONAL,
        family_id=family_id,
        strategy_id="paper_session",
        experiment_id=experiment_id,
        decision=DiscoveryDecisionKind.BLOCKED_HARD,
        stage=DiscoveryStage.DATA,
        reason_codes=(ReasonCode.DATA_FEED_ERROR,),
        reason_text=reason_text,
        profile_version=profile_version,
        code_version=code_version,
        inputs=DiscoveryDecisionInputs(),
    )
    return store.append(
        TradingEventType.DISCOVERY_DECISION,
        record,
        event_id=record.decision_id,
        recorded_at=as_of,
    )


def persist_exit_decision(
    store: TradingStore,
    id_factory: IdFactory,
    *,
    cycle_id: str,
    as_of: datetime,
    profile_version: str,
    code_version: str,
    intent: object,
    position: PositionState,
    exit_kind: ExitKind,
    detail: str,
    monitor_price: Price | None,
    realized_pnl: Money | None,
) -> int:
    """Append one EXIT-stage DISCOVERY_DECISION when a managed exit fires."""
    trade_intent = intent if isinstance(intent, TradeIntent) else None
    mode_id = (
        trade_intent.mode_id
        if trade_intent is not None and trade_intent.mode_id is not None
        else ModeId.M2_DIRECTIONAL
    )
    family_key = (
        trade_intent.family_id
        if trade_intent is not None and trade_intent.family_id is not None
        else "unknown"
    )
    experiment_id = (
        trade_intent.experiment_id if trade_intent is not None else "EXP-UNKNOWN"
    )
    strategy_id = trade_intent.strategy_id if trade_intent is not None else "unknown"
    rule = exit_kind.value.lower()
    ctx = DecisionTextContext(
        exit_rule=rule,
        exit_price=str(monitor_price.value) if monitor_price is not None else None,
        realized_pnl=str(realized_pnl.amount) if realized_pnl is not None else None,
        detail=detail,
    )
    reason_text = (
        f"Exit {rule}: {detail}; "
        f"price {ctx.exit_price or 'n/a'}, P&L {ctx.realized_pnl or 'n/a'}."
    )
    record = DiscoveryDecision(
        decision_id=id_factory.new_id("DDEC"),
        cycle_id=cycle_id,
        as_of=as_of,
        mode_id=mode_id,
        family_id=family_key,
        strategy_id=strategy_id,
        experiment_id=experiment_id,
        decision=DiscoveryDecisionKind.NO_TRADE,
        stage=DiscoveryStage.EXIT,
        reason_codes=(ReasonCode.OK,),
        reason_text=reason_text,
        profile_version=profile_version,
        code_version=code_version,
        inputs=DiscoveryDecisionInputs(),
        exit_rule=rule,
        exit_price=monitor_price,
        realized_pnl=realized_pnl,
    )
    return store.append(
        TradingEventType.DISCOVERY_DECISION,
        record,
        event_id=record.decision_id,
        recorded_at=as_of,
    )


def query_decisions(
    store: TradingStore,
    *,
    session_date: datetime | None = None,
    mode_id: ModeId | None = None,
    experiment_id: str | None = None,
) -> tuple[DiscoveryDecision, ...]:
    """Read DISCOVERY_DECISION events with optional filters."""
    kol = ZoneInfo("Asia/Kolkata")
    rows: list[DiscoveryDecision] = []
    for event in store.read_events():
        if event.event_type is not TradingEventType.DISCOVERY_DECISION:
            continue
        payload = event.deserialize()
        if not isinstance(payload, DiscoveryDecision):
            continue
        if session_date is not None:
            local = payload.as_of.astimezone(kol).date()
            if local != session_date.astimezone(kol).date():
                continue
        if mode_id is not None and payload.mode_id is not mode_id:
            continue
        if experiment_id is not None and payload.experiment_id != experiment_id:
            continue
        rows.append(payload)
    return tuple(rows)


def build_decision_record(
    request: PaperStrategyRequest,
    outcome: PaperStrategyOutcome,
    *,
    cycle_id: str,
    as_of: datetime,
    profile_version: str,
    code_version: str,
    entry_profile: EntryProfile,
    discovery_config: DiscoveryConfig | None,
    id_factory: IdFactory,
    market_state: MarketState | None = None,
    arbitration_result: object | None = None,
) -> DiscoveryDecision:
    """Build one evaluation record from a request/outcome pair."""
    mode_id = outcome.mode_id or request.forced_mode_id or ModeId.M2_DIRECTIONAL
    family_key = (
        outcome.family_id.value
        if outcome.family_id is not None
        else (
            request.forced_family_id.value
            if request.forced_family_id is not None
            else "unknown"
        )
    )
    reason_codes = _collect_reason_codes(outcome, arbitration_result=arbitration_result)
    stage = _infer_stage(request, outcome, reason_codes)
    decision_kind = _infer_decision_kind(
        outcome,
        reason_codes,
        entry_profile=entry_profile,
        discovery_config=discovery_config,
    )
    strict_would_block = tuple(
        dict.fromkeys((*outcome.strict_would_block, *_soft_shadows(outcome)))
    )
    inputs = _build_inputs(request, outcome, market_state=market_state)
    candidates = _build_candidates(request, outcome)
    sizing, fill = _build_trade_snapshots(outcome)
    ctx = _text_context(request, outcome, reason_codes, inputs)
    reason_text = render_decision_text(
        decision=decision_kind,
        reason_codes=reason_codes
        or (
            (ReasonCode.OK,)
            if decision_kind is DiscoveryDecisionKind.TRADE
            else (ReasonCode.INSTRUMENT_UNKNOWN,)
        ),
        ctx=ctx,
    )
    return DiscoveryDecision(
        decision_id=id_factory.new_id("DDEC"),
        cycle_id=cycle_id,
        as_of=as_of,
        mode_id=mode_id,
        family_id=family_key,
        strategy_id=outcome.strategy_id,
        experiment_id=outcome.experiment_id or request.experiment_id,
        decision=decision_kind,
        stage=stage,
        reason_codes=reason_codes
        or (
            (ReasonCode.OK,)
            if decision_kind is DiscoveryDecisionKind.TRADE
            else (ReasonCode.INSTRUMENT_UNKNOWN,)
        ),
        reason_text=reason_text,
        strict_would_block=strict_would_block,
        profile_version=profile_version,
        code_version=code_version,
        inputs=inputs,
        candidates=candidates,
        sizing=sizing,
        fill=fill,
    )


def _collect_reason_codes(
    outcome: PaperStrategyOutcome,
    *,
    arbitration_result: object | None,
) -> tuple[ReasonCode, ...]:
    if outcome.order_events:
        return (ReasonCode.OK,)
    codes: list[ReasonCode] = []
    if outcome.binding_reason_codes and not outcome.intents:
        codes.extend(outcome.binding_reason_codes)
    if outcome.entry_blocked_reasons:
        codes.extend(outcome.entry_blocked_reasons)
    if outcome.rejection_reasons:
        codes.extend(outcome.rejection_reasons)
    for decision in outcome.decisions:
        if decision.action is RiskAction.REJECT:
            codes.extend(decision.reason_codes)
    if not codes and not outcome.intents:
        codes.append(ReasonCode.INSTRUMENT_UNKNOWN)
    return tuple(dict.fromkeys(codes))


def _infer_stage(  # noqa: PLR0911
    request: PaperStrategyRequest,
    outcome: PaperStrategyOutcome,
    reason_codes: tuple[ReasonCode, ...],
) -> DiscoveryStage:
    if outcome.record_stage is not None:
        return outcome.record_stage
    if outcome.order_events:
        return DiscoveryStage.FILL
    if any(code in _DIRECTION_CODES for code in reason_codes):
        return DiscoveryStage.DIRECTION
    if not request.candidates and request.binding_reason_codes and not outcome.intents:
        return DiscoveryStage.BIND
    if outcome.entry_blocked_reasons or any(
        code in _HARD_BLOCK_CODES for code in reason_codes
    ):
        if any(code in _HARD_BLOCK_CODES for code in reason_codes):
            return DiscoveryStage.DATA
        return DiscoveryStage.RISK
    for decision in outcome.decisions:
        if decision.action is RiskAction.REJECT:
            return DiscoveryStage.RISK
    if outcome.intents and not outcome.order_events:
        suppressed = any(
            code
            in {
                ReasonCode.EXACT_DUPLICATE_SUPPRESSED,
                ReasonCode.ECONOMIC_OVERLAP_SUPPRESSED,
                ReasonCode.OPPOSING_EXPOSURE_REJECTED,
                ReasonCode.DAILY_ENTRY_CAP,
                ReasonCode.M4_POSITION_CAP_REACHED,
            }
            for code in reason_codes
        )
        if suppressed:
            return DiscoveryStage.ARBITER
    return DiscoveryStage.STRATEGY


def _infer_decision_kind(
    outcome: PaperStrategyOutcome,
    reason_codes: tuple[ReasonCode, ...],
    *,
    entry_profile: EntryProfile,
    discovery_config: DiscoveryConfig | None,
) -> DiscoveryDecisionKind:
    if outcome.order_events:
        return DiscoveryDecisionKind.TRADE
    for code in reason_codes:
        if code is ReasonCode.OK:
            continue
        if not is_soft(code, entry_profile, discovery_config):
            return DiscoveryDecisionKind.BLOCKED_HARD
    return DiscoveryDecisionKind.NO_TRADE


def _soft_shadows(outcome: PaperStrategyOutcome) -> tuple[ReasonCode, ...]:
    shadows: list[ReasonCode] = []
    for decision in outcome.decisions:
        shadows.extend(decision.strict_would_block)
    return tuple(dict.fromkeys(shadows))


def _build_inputs(
    request: PaperStrategyRequest,
    outcome: PaperStrategyOutcome,
    *,
    market_state: MarketState | None,
) -> DiscoveryDecisionInputs:
    underlying = request.underlying
    setup = outcome.setup_features or request.setup_features
    spot = underlying.market.last.value if underlying.market.last is not None else None
    trend = None
    return_15m = None
    return_60m = None
    trend_score = None
    iv_bucket = None
    event_state = None
    direction_fallback = False
    if setup is not None:
        trend = setup.trend.value
        return_15m = setup.score_components.get("return_15m")
        return_60m = setup.score_components.get("return_60m")
        trend_score = setup.raw_setup_score
        iv_bucket = setup.volatility.value
    elif market_state is not None:
        trend = market_state.trend.value
        return_15m = market_state.return_15m
        return_60m = market_state.return_60m
        trend_score = market_state.trend_score
        iv_bucket = market_state.volatility.value
        event_state = market_state.event_state
        direction_fallback = ReasonCode.DIRECTION_FALLBACK in market_state.reason_codes
    quote_age_ms = None
    times = underlying.times
    if times is not None:
        quote_age_ms = max(
            0,
            int((times.calculation_time - times.event_time).total_seconds() * 1000),
        )
    return DiscoveryDecisionInputs(
        spot=spot,
        trend=trend,
        direction_fallback=direction_fallback,
        iv_bucket=iv_bucket,
        event_state=event_state,
        quote_age_ms=quote_age_ms,
        return_15m=return_15m,
        return_60m=return_60m,
        trend_score=trend_score,
    )


def _build_candidates(
    request: PaperStrategyRequest,
    outcome: PaperStrategyOutcome,
) -> tuple[DiscoveryCandidateRow, ...]:
    selected = {
        leg.contract.symbol for intent in outcome.intents for leg in intent.legs
    }
    return tuple(
        _candidate_row(snap, selected=snap.contract.symbol in selected)
        for snap in request.candidates
    )


def _candidate_row(
    snap: FeatureSnapshot,
    *,
    selected: bool,
    rejection: ReasonCode | None = None,
) -> DiscoveryCandidateRow:
    delta = None
    oi = None
    spread = None
    if snap.derivatives is not None:
        if snap.derivatives.greeks is not None:
            delta = snap.derivatives.greeks.delta
        oi = snap.derivatives.open_interest
    bid = snap.market.bid
    ask = snap.market.ask
    if bid is not None and ask is not None and bid.value > 0:
        mid = (bid.value + ask.value) / Decimal("2")
        if mid > 0:
            spread = (ask.value - bid.value) / mid
    return DiscoveryCandidateRow(
        symbol=snap.contract.symbol,
        delta=delta,
        open_interest=oi,
        spread_fraction=spread,
        selected=selected,
        rejection_reason=rejection,
    )


def _build_trade_snapshots(
    outcome: PaperStrategyOutcome,
) -> tuple[DiscoverySizingSnapshot | None, DiscoveryFillSnapshot | None]:
    if not outcome.decisions and not outcome.order_events:
        return None, None
    sizing: DiscoverySizingSnapshot | None = None
    fill: DiscoveryFillSnapshot | None = None
    approved = next(
        (item for item in outcome.decisions if item.action is RiskAction.APPROVE),
        None,
    )
    if approved is None:
        approved = next(
            (item for item in outcome.decisions if item.action is RiskAction.RESIZE),
            None,
        )
    if approved is not None:
        tags = approved.applied_limits
        lots = approved.approved_legs[0].lots.count if approved.approved_legs else None
        sizing = DiscoverySizingSnapshot(
            mode_equity=approved.reserved_capital,
            sizing_guide=approved.recalculated_max_loss,
            one_lot_loss=approved.recalculated_max_loss,
            lots=lots,
            tags=tags,
        )
    if outcome.order_events:
        event = outcome.order_events[0]
        fill = DiscoveryFillSnapshot(
            assumed_price=event.average_fill_price,
            strict_verdict=event.strict_fill_verdict,
        )
    return sizing, fill


def _text_context(
    request: PaperStrategyRequest,
    outcome: PaperStrategyOutcome,
    reason_codes: tuple[ReasonCode, ...],
    inputs: DiscoveryDecisionInputs,
) -> DecisionTextContext:
    detail = outcome.rejection_details[0] if outcome.rejection_details else None
    conviction = request.underlying.features.get("conviction_score")
    return DecisionTextContext(
        return_15m=inputs.return_15m,
        return_60m=inputs.return_60m,
        trend_score=inputs.trend_score,
        threshold=_NEUTRAL_THRESHOLD,
        conviction_score=conviction if isinstance(conviction, Decimal) else None,
        detail=detail,
        mode_id=(outcome.mode_id.value if outcome.mode_id is not None else None),
        family_id=(outcome.family_id.value if outcome.family_id is not None else None),
    )
