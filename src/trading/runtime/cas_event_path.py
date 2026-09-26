"""Event-driven CAS (M1) entry path with latency measurement (Gate G3)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, time
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

from trading.domain.contracts import FeatureSnapshot
from trading.domain.enums import ExecutionMode, ReasonCode
from trading.identification import top_book_size

_IST = ZoneInfo("Asia/Kolkata")
ORACLE_MEASURED = "oracle_measured"
SIMULATED = "simulated"

__all__ = [
    "CAS_HOLDING_SECONDS",
    "CasEventDrivenConfig",
    "CasEventEvaluation",
    "CasLatencyReport",
    "M1EventGateOutcome",
    "M1ProviderEvent",
    "M1ScanWindow",
    "active_m1_window",
    "cas_event_paper_permitted",
    "evaluate_m1_provider_event",
    "explain_m1_strike",
    "ledger_block_reason",
    "load_measured_latency_report",
    "measure_cas_entry_latency",
    "measured_report_passes",
    "record_episode_attempt",
]

CAS_HOLDING_SECONDS = 900


class M1ScanWindow(BaseModel):
    """One M1 scan window. Closing context is NFO continuous trade, not cash CAS."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    window_id: str
    start_ist: str
    end_ist: str
    profile_label: str


def _default_scan_windows() -> tuple[M1ScanWindow, ...]:
    return (
        M1ScanWindow(
            window_id="continuous",
            start_ist="09:20",
            end_ist="15:00",
            profile_label="continuous",
        ),
        M1ScanWindow(
            window_id="closing_context",
            start_ist="15:00",
            end_ist="15:25",
            profile_label="closing_context",
        ),
    )


class CasEventDrivenConfig(BaseModel):
    """Honest M1 event path. Thresholds are acceptance limits, not fitted results.

    Chosen before any Oracle sample was read:
    quote age 500 ms, signal-to-decision 2000 ms, paper execution 2000 ms,
    exit-monitor gap 2000 ms.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = False
    profile: str = "quote_only"  # quote_only | depth_capable
    quote_max_age_ms: int = Field(default=500, ge=1)
    max_entry_latency_ms: int = Field(default=2000, ge=1)
    max_execution_latency_ms: int = Field(default=2000, ge=1)
    max_exit_monitor_gap_ms: int = Field(default=2000, ge=1)
    microprice_dislocation_ticks: Decimal = Field(default=Decimal("2"))
    min_top_book_size: int = Field(default=1, ge=1)
    daily_loss_budget_fraction: Decimal = Field(default=Decimal("0.15"), ge=0, le=1)
    max_attempts_per_session: int = Field(default=20, ge=1)
    episode_retry_limit: int = Field(default=1, ge=1)
    prefer_delta_min: Decimal = Field(default=Decimal("0.15"))
    prefer_delta_max: Decimal = Field(default=Decimal("0.40"))
    measured_latency_path: str = "data/paper/m1_latency_report.json"
    scan_windows: tuple[M1ScanWindow, ...] = Field(
        default_factory=_default_scan_windows
    )


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
    provenance: str = "unlabelled"
    quote_age_p95_ms: int | None = None
    execution_p95_ms: int | None = None
    exit_gap_p95_ms: int | None = None


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
    if config.profile in {"depth_only", "depth_capable"} and not depth_observed:
        return CasEventEvaluation(
            triggered=False,
            profile=config.profile,
            quote_age_ms=age_ms,
            microprice_edge_ticks=Decimal(str(edge_ticks)),
            depth_observed=False,
            reason_codes=(ReasonCode.DATA_GAP,),
            detail="depth_only profile requires observed book size",
        )
    # quote_only: age is the hard gate. Size-balanced top-of-book cannot
    # produce a multi-tick microprice edge on a one-tick spread, so do not
    # require dislocation unless an imbalanced book is actually observed.
    balanced_book = (
        quote.bid_size is not None
        and quote.ask_size is not None
        and quote.bid_size == quote.ask_size
    )
    require_edge = config.profile != "quote_only" or (
        depth_observed and not balanced_book
    )
    if require_edge and edge_ticks < config.microprice_dislocation_ticks:
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


def _p95(samples: tuple[int, ...]) -> int | None:
    if not samples:
        return None
    ordered = sorted(samples)
    return ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]


def measure_cas_entry_latency(
    samples_ms: tuple[int, ...],
    *,
    config: CasEventDrivenConfig,
    provenance: str = "unlabelled",
    quote_ages_ms: tuple[int, ...] = (),
    execution_latencies_ms: tuple[int, ...] = (),
    exit_monitor_gaps_ms: tuple[int, ...] = (),
) -> CasLatencyReport:
    """Aggregate latency samples against the predeclared session gate.

    Unlabelled or partial samples fail. A pass requires oracle_measured
    provenance and every series inside its threshold.
    """
    quote_p95 = _p95(quote_ages_ms)
    execution_p95 = _p95(execution_latencies_ms)
    exit_p95 = _p95(exit_monitor_gaps_ms)
    if not samples_ms:
        return CasLatencyReport(
            samples=0,
            p50_ms=None,
            p95_ms=None,
            max_ms=None,
            quote_max_age_ms=config.quote_max_age_ms,
            passes_session_gate=False,
            detail="no latency samples",
            provenance=provenance,
            quote_age_p95_ms=quote_p95,
            execution_p95_ms=execution_p95,
            exit_gap_p95_ms=exit_p95,
        )
    ordered = sorted(samples_ms)
    p50 = ordered[len(ordered) // 2]
    p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
    max_ms = ordered[-1]
    decision_ok = (
        max_ms <= config.max_entry_latency_ms and p95 <= config.max_entry_latency_ms
    )
    quote_ok = quote_p95 is not None and quote_p95 <= config.quote_max_age_ms
    execution_ok = (
        execution_p95 is not None and execution_p95 <= config.max_execution_latency_ms
    )
    exit_ok = exit_p95 is not None and exit_p95 <= config.max_exit_monitor_gap_ms
    passes = (
        provenance == ORACLE_MEASURED
        and decision_ok
        and quote_ok
        and execution_ok
        and exit_ok
    )
    if passes:
        detail = "session gate passed on oracle-measured samples"
    elif provenance != ORACLE_MEASURED:
        detail = f"provenance {provenance} is not oracle_measured"
    else:
        detail = (
            f"decision p95 {p95}ms max {max_ms}ms, quote p95 {quote_p95}ms, "
            f"execution p95 {execution_p95}ms, exit gap p95 {exit_p95}ms"
        )
    return CasLatencyReport(
        samples=len(samples_ms),
        p50_ms=p50,
        p95_ms=p95,
        max_ms=max_ms,
        quote_max_age_ms=config.quote_max_age_ms,
        passes_session_gate=passes,
        detail=detail,
        provenance=provenance,
        quote_age_p95_ms=quote_p95,
        execution_p95_ms=execution_p95,
        exit_gap_p95_ms=exit_p95,
    )


@dataclass(frozen=True, slots=True)
class M1EventGateOutcome:
    """Whether one M1 producer row may execute from a provider event."""

    execute: bool
    execution_mode: ExecutionMode
    ledger_block: ReasonCode | None
    explained: bool
    detail: str
    latency_limitation: str | None = None


def evaluate_m1_provider_event(
    *,
    item: object,
    event: M1ProviderEvent | None,
    option_candidates: tuple[FeatureSnapshot, ...],
    config: CasEventDrivenConfig,
    stance: ExecutionMode,
    attempts_today: int,
    in_window: bool,
    latency_report: CasLatencyReport,
    session_date: str,
    episode_ledger: Path,
    mode_capital: Decimal,
) -> M1EventGateOutcome:
    """Apply the session M1 gate to one produced family row."""
    block: ReasonCode | None = None
    episode_id = ""
    if isinstance(event, M1ProviderEvent):
        raw_episode = event.episode_id
        family_id = getattr(getattr(item, "spec", None), "family_id", None)
        if raw_episode:
            episode_id = raw_episode
        elif family_id is not None and hasattr(family_id, "value"):
            episode_id = str(family_id.value)
        if event.disconnected or (
            not event.exchange_timestamp_observed and not event.allow_simulated_fixture
        ):
            block = ReasonCode.DATA_GAP
        else:
            block = ledger_block_reason(
                episode_ledger,
                session_date=session_date,
                episode_id=episode_id,
                mode_capital=mode_capital,
                config=config,
            )
    bound = getattr(item, "bound", None)
    selected = () if bound is None else getattr(bound, "candidates", ())[:1]
    explained = True
    detail = ""
    if selected:
        explained, detail = explain_m1_strike(
            selected[0],
            option_candidates,
            config=config,
        )
    permitted, mode = cas_event_paper_permitted(
        config=config,
        latency_report=latency_report,
        stance=stance,
        attempts_today=attempts_today,
        in_window=in_window,
        event_provenance=(
            event.provenance if isinstance(event, M1ProviderEvent) else "unlabelled"
        ),
        allow_simulated_fixture=(
            event.allow_simulated_fixture
            if isinstance(event, M1ProviderEvent)
            else False
        ),
        ledger_block=block,
    )
    trigger_block: ReasonCode | None = None
    if (
        permitted
        and isinstance(event, M1ProviderEvent)
        and selected
        and not event.disconnected
    ):
        event_at = event.quote_time or event.event_time
        if event_at is None and not event.allow_simulated_fixture:
            trigger_block = ReasonCode.DATA_GAP
            permitted = False
        else:
            trigger = evaluate_cas_event_trigger(
                selected[0],
                config=config,
                event_at=event_at or event.receive_time,
                now=event.decided_at,
            )
            if not trigger.triggered:
                trigger_block = (
                    trigger.reason_codes[0]
                    if trigger.reason_codes
                    else ReasonCode.SETUP_COOLDOWN
                )
                permitted = False
                if not detail:
                    detail = trigger.detail
    if trigger_block is not None and block is None:
        block = trigger_block
    latency_limitation = None
    if permitted and not latency_report.passes_session_gate:
        latency_limitation = latency_report.detail
    binding = getattr(bound, "binding", None)
    eligible = getattr(binding, "eligible", False) if binding is not None else False
    execute = (
        permitted
        and eligible
        and explained
        and isinstance(event, M1ProviderEvent)
        and not event.disconnected
    )
    return M1EventGateOutcome(
        execute=execute,
        execution_mode=mode if execute else ExecutionMode.SHADOW,
        ledger_block=block,
        explained=explained,
        detail=detail,
        latency_limitation=latency_limitation,
    )


def cas_event_paper_permitted(
    *,
    config: CasEventDrivenConfig,
    latency_report: CasLatencyReport,
    stance: ExecutionMode,
    attempts_today: int,
    in_window: bool = True,
    event_provenance: str = "unlabelled",
    allow_simulated_fixture: bool = False,
    ledger_block: ReasonCode | None = None,
) -> tuple[bool, ExecutionMode]:
    """Return whether an M1 provider event may execute in PAPER.

    Latency samples are measured separately; they do not demote or block PAPER.
    """
    _ = latency_report, event_provenance, allow_simulated_fixture
    if stance is not ExecutionMode.PAPER or not config.enabled or not in_window:
        return False, ExecutionMode.SHADOW
    if ledger_block is not None or attempts_today >= config.max_attempts_per_session:
        return False, ExecutionMode.SHADOW
    return True, ExecutionMode.PAPER


@dataclass(frozen=True, slots=True)
class M1ProviderEvent:
    """One provider event. Missing timestamps stay None; they are not invented."""

    receive_time: datetime
    decided_at: datetime
    provenance: str
    profile_observed: str
    depth_fields_present: bool
    event_time: datetime | None = None
    quote_time: datetime | None = None
    submitted_at: datetime | None = None
    execution_completed_at: datetime | None = None
    previous_exit_quote_at: datetime | None = None
    exit_quote_at: datetime | None = None
    episode_id: str = ""
    disconnected: bool = False
    allow_simulated_fixture: bool = False
    exchange_timestamp_observed: bool = False


def _parse_hhmm(value: str) -> time:
    hour, minute = value.split(":")
    return time(int(hour), int(minute))


def active_m1_window(
    now: datetime, config: CasEventDrivenConfig
) -> M1ScanWindow | None:
    """Return the scan window containing ``now``. Cash auction 15:30-15:40 is absent."""
    local = now.astimezone(_IST).time()
    for window in config.scan_windows:
        start = _parse_hhmm(window.start_ist)
        end = _parse_hhmm(window.end_ist)
        if start <= local < end:
            return window
    return None


def explain_m1_strike(
    selected: FeatureSnapshot,
    alternatives: tuple[FeatureSnapshot, ...],
    *,
    config: CasEventDrivenConfig,
) -> tuple[bool, str]:
    """Say why the selected strike beat the others. Cheap premium is not a reason."""
    selected_delta = _abs_delta(selected)
    if selected_delta is None:
        return False, "selected strike has no observed delta"
    if (
        selected_delta < config.prefer_delta_min
        or selected_delta > config.prefer_delta_max
    ):
        return (
            False,
            f"selected absolute delta {selected_delta} is outside "
            f"{config.prefer_delta_min}-{config.prefer_delta_max}",
        )
    selected_ask = _ask(selected)
    if selected_ask is None or selected_ask <= 0:
        return False, "selected strike has no ask"
    selected_response = selected_delta / selected_ask
    notes = [
        f"selected {selected.contract.symbol} delta {selected_delta} "
        f"response {selected_response} ask {selected_ask}"
    ]
    for item in alternatives:
        if item.contract.symbol == selected.contract.symbol:
            continue
        delta = _abs_delta(item)
        ask = _ask(item)
        if delta is None or ask is None or ask <= 0:
            notes.append(f"{item.contract.symbol} skipped: missing delta or ask")
            continue
        response = delta / ask
        if ask < selected_ask and delta < config.prefer_delta_min:
            notes.append(
                f"{item.contract.symbol} cheaper ask {ask} but delta {delta} "
                "is extreme OTM and was not selected for premium"
            )
            continue
        if (
            response > selected_response
            and config.prefer_delta_min <= delta <= config.prefer_delta_max
        ):
            return (
                False,
                f"{item.contract.symbol} response {response} beats selected "
                f"{selected_response} inside the prefer band",
            )
        notes.append(
            f"{item.contract.symbol} delta {delta} response {response} "
            "did not beat selected"
        )
    return True, "; ".join(notes)


def _abs_delta(candidate: FeatureSnapshot) -> Decimal | None:
    derivatives = candidate.derivatives
    if derivatives is None or derivatives.greeks is None:
        return None
    delta = derivatives.greeks.delta
    return None if delta is None else abs(delta)


def _ask(candidate: FeatureSnapshot) -> Decimal | None:
    ask = candidate.market.ask
    return None if ask is None else ask.value


def load_measured_latency_report(
    path: Path, *, config: CasEventDrivenConfig
) -> CasLatencyReport | None:
    """Load a report written from provider timestamps. A missing file is not a pass."""
    if not path.is_file():
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("provenance") != ORACLE_MEASURED:
        return None
    return measure_cas_entry_latency(
        tuple(int(item) for item in raw.get("decision_latencies_ms", ())),
        config=config,
        provenance=ORACLE_MEASURED,
        quote_ages_ms=tuple(int(item) for item in raw.get("quote_ages_ms", ())),
        execution_latencies_ms=tuple(
            int(item) for item in raw.get("execution_latencies_ms", ())
        ),
        exit_monitor_gaps_ms=tuple(
            int(item) for item in raw.get("exit_monitor_gaps_ms", ())
        ),
    )


def measured_report_passes(
    config: CasEventDrivenConfig, *, repo_root: Path | None = None
) -> bool:
    """True only when the on-disk Oracle report meets the predeclared thresholds."""
    root = repo_root or Path.cwd()
    report = load_measured_latency_report(
        root / config.measured_latency_path, config=config
    )
    return report is not None and report.passes_session_gate


def ledger_block_reason(
    path: Path,
    *,
    session_date: str,
    episode_id: str,
    mode_capital: Decimal,
    config: CasEventDrivenConfig,
) -> ReasonCode | None:
    """Block a new M1 attempt when the persisted episode or daily loss cap is hit."""
    if not path.is_file():
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        return ReasonCode.DATA_INVALID
    day = raw.get(session_date, {})
    if not isinstance(day, dict):
        return None
    attempts = day.get("episodes", {})
    if isinstance(attempts, dict):
        count = int(attempts.get(episode_id, 0))
        if episode_id and count >= config.episode_retry_limit:
            return ReasonCode.SETUP_COOLDOWN
    loss = Decimal(str(day.get("loss_inr", "0")))
    budget = mode_capital * config.daily_loss_budget_fraction
    if loss >= budget > 0:
        return ReasonCode.RISK_LIMIT_DAILY_LOSS
    return None


def record_episode_attempt(
    path: Path,
    *,
    session_date: str,
    episode_id: str,
    loss_inr: Decimal = Decimal("0"),
) -> None:
    """Persist an M1 attempt so a restart still sees the episode and the loss."""
    raw: dict[str, object] = {}
    if path.is_file():
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            raw = loaded
    day = raw.get(session_date)
    if not isinstance(day, dict):
        day = {"episodes": {}, "loss_inr": "0"}
    episodes = day.get("episodes")
    if not isinstance(episodes, dict):
        episodes = {}
    episodes[episode_id] = int(episodes.get(episode_id, 0)) + 1
    day["episodes"] = episodes
    prior = Decimal(str(day.get("loss_inr", "0")))
    day["loss_inr"] = str(prior + loss_inr)
    raw[session_date] = day
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(raw), encoding="utf-8")
