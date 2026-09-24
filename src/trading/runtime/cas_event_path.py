"""Event-driven CAS (M1) entry path with latency measurement (Gate G3)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from trading.domain.contracts import FeatureSnapshot
from trading.domain.enums import ExecutionMode, ReasonCode
from trading.identification import top_book_size

__all__ = [
    "CAS_HOLDING_SECONDS",
    "CasEventDrivenConfig",
    "CasEventEvaluation",
    "CasLatencyReport",
    "evaluate_cas_event_trigger",
    "measure_cas_entry_latency",
]

CAS_HOLDING_SECONDS = 900


class CasEventDrivenConfig(BaseModel):
    """Honest M1 event path — quote-only or depth-augmented, never aggressor."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = False
    profile: str = "quote_only"  # quote_only | depth_only
    quote_max_age_ms: int = Field(default=500, ge=1)
    max_entry_latency_ms: int = Field(default=2000, ge=1)
    microprice_dislocation_ticks: Decimal = Field(default=Decimal("2"))
    min_top_book_size: int = Field(default=1, ge=1)
    daily_loss_budget_fraction: Decimal = Field(default=Decimal("0.15"), ge=0, le=1)
    max_attempts_per_session: int = Field(default=20, ge=1)


@dataclass(frozen=True, slots=True)
class CasEventEvaluation:
    """One CAS trigger evaluation from a fresh quote event."""

    triggered: bool
    profile: str
    quote_age_ms: int | None
    microprice_edge_ticks: Decimal | None
    depth_observed: bool
    reason_codes: tuple[ReasonCode, ...]
    detail: str


@dataclass(frozen=True, slots=True)
class CasLatencyReport:
    """Measured decision latency for session acceptance."""

    samples: int
    p50_ms: int | None
    p95_ms: int | None
    max_ms: int | None
    quote_max_age_ms: int
    passes_session_gate: bool
    detail: str


def evaluate_cas_event_trigger(
    candidate: FeatureSnapshot,
    *,
    config: CasEventDrivenConfig,
    event_at: datetime,
    now: datetime,
) -> CasEventEvaluation:
    """Fail closed unless quote is fresh and microprice edge exceeds threshold."""
    quote = candidate.market
    if quote.bid is None or quote.ask is None or quote.last is None:
        return CasEventEvaluation(
            triggered=False,
            profile=config.profile,
            quote_age_ms=None,
            microprice_edge_ticks=None,
            depth_observed=False,
            reason_codes=(ReasonCode.DATA_INVALID,),
            detail="missing bid/ask/last",
        )
    age_ms = int((now - event_at).total_seconds() * 1000)
    if age_ms > config.quote_max_age_ms:
        return CasEventEvaluation(
            triggered=False,
            profile=config.profile,
            quote_age_ms=age_ms,
            microprice_edge_ticks=None,
            depth_observed=False,
            reason_codes=(ReasonCode.DATA_STALE,),
            detail=f"quote age {age_ms}ms > {config.quote_max_age_ms}ms",
        )
    bid = quote.bid.value
    ask = quote.ask.value
    if bid <= 0 or ask <= 0 or ask < bid:
        return CasEventEvaluation(
            triggered=False,
            profile=config.profile,
            quote_age_ms=age_ms,
            microprice_edge_ticks=None,
            depth_observed=False,
            reason_codes=(ReasonCode.DATA_INVALID,),
            detail="invalid bid/ask",
        )
    mid = (bid + ask) / 2
    microprice = (
        (ask * quote.bid_size + bid * quote.ask_size)
        / (quote.bid_size + quote.ask_size)
        if quote.bid_size and quote.ask_size
        else mid
    )
    tick = Decimal("0.05")
    edge_ticks = abs(microprice - mid) / tick
    depth_observed = top_book_size(candidate) is not None
    if config.profile == "depth_only" and not depth_observed:
        return CasEventEvaluation(
            triggered=False,
            profile=config.profile,
            quote_age_ms=age_ms,
            microprice_edge_ticks=Decimal(str(edge_ticks)),
            depth_observed=False,
            reason_codes=(ReasonCode.DATA_GAP,),
            detail="depth_only profile requires observed book size",
        )
    if edge_ticks < config.microprice_dislocation_ticks:
        return CasEventEvaluation(
            triggered=False,
            profile=config.profile,
            quote_age_ms=age_ms,
            microprice_edge_ticks=Decimal(str(edge_ticks)),
            depth_observed=depth_observed,
            reason_codes=(ReasonCode.SETUP_COOLDOWN,),
            detail="microprice edge below threshold",
        )
    return CasEventEvaluation(
        triggered=True,
        profile=config.profile,
        quote_age_ms=age_ms,
        microprice_edge_ticks=Decimal(str(edge_ticks)),
        depth_observed=depth_observed,
        reason_codes=(),
        detail="event trigger satisfied",
    )


def measure_cas_entry_latency(
    samples_ms: tuple[int, ...],
    *,
    config: CasEventDrivenConfig,
) -> CasLatencyReport:
    """Aggregate latency samples against the session gate."""
    if not samples_ms:
        return CasLatencyReport(
            samples=0,
            p50_ms=None,
            p95_ms=None,
            max_ms=None,
            quote_max_age_ms=config.quote_max_age_ms,
            passes_session_gate=False,
            detail="no latency samples",
        )
    ordered = sorted(samples_ms)
    p50 = ordered[len(ordered) // 2]
    p95 = ordered[int(len(ordered) * 0.95)]
    max_ms = ordered[-1]
    passes = (
        max_ms <= config.max_entry_latency_ms and p95 <= config.max_entry_latency_ms
    )
    return CasLatencyReport(
        samples=len(samples_ms),
        p50_ms=p50,
        p95_ms=p95,
        max_ms=max_ms,
        quote_max_age_ms=config.quote_max_age_ms,
        passes_session_gate=passes,
        detail=(
            "session gate passed"
            if passes
            else f"max {max_ms}ms or p95 {p95}ms exceeds {config.max_entry_latency_ms}ms"
        ),
    )


def cas_event_paper_permitted(
    *,
    config: CasEventDrivenConfig,
    latency_report: CasLatencyReport,
    stance: ExecutionMode,
    attempts_today: int,
) -> tuple[bool, ExecutionMode]:
    """M1 PAPER only when event path is enabled and latency gate passes."""
    if not config.enabled or not latency_report.passes_session_gate:
        return False, ExecutionMode.SHADOW
    if stance is not ExecutionMode.PAPER:
        return False, ExecutionMode.SHADOW
    if attempts_today >= config.max_attempts_per_session:
        return False, ExecutionMode.SHADOW
    return True, ExecutionMode.PAPER
