"""Unattended PAPER session: Telegram auth, then L1→L3→L2 against the paper broker."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import date, datetime, timedelta
from datetime import time as dt_time
from decimal import Decimal
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, ConfigDict, Field

from trading.broker.paper import PaperBroker
from trading.broker.ports import BrokerFunds
from trading.config import load_config, load_evaluation_config, load_risk_policy
from trading.config.discovery import DiscoveryConfig, load_discovery_config
from trading.config.paper_data import load_paper_data_requirements
from trading.config.risk_policy import RiskPolicyConfig
from trading.config.schema import Environment
from trading.data.config import DataPipelineConfig, load_data_pipeline_config
from trading.data.events import CanonicalMarketEvent, RawMarketCapture
from trading.data.fyers.auth import run_telegram_auth
from trading.data.fyers.client import FyersApiError, FyersMarketFeed
from trading.data.fyers.telegram import send_telegram_message, telegram_configured
from trading.data.normalize import (
    normalize_fyers_depth,
    normalize_fyers_option_chain,
    normalize_fyers_quotes,
)
from trading.data.pipeline import build_pipeline
from trading.data.prices import depth_top_sizes, observed_book_sizes, optional_int_qty
from trading.data.settings import FyersSettings
from trading.data.storage.instrument_store import InstrumentSpecStore
from trading.data.storage.snapshot_store import SnapshotStore
from trading.domain.clock import Clock, WallClock
from trading.domain.contracts import FeatureSnapshot, InstrumentSpec
from trading.domain.contracts.identification import MarketState
from trading.domain.contracts.mode_policy import ModesConfig, load_modes_config
from trading.domain.contracts.paper_data import PaperDataRequirements
from trading.domain.contracts.snapshot import MarketQuote
from trading.domain.enums import (
    EntryProfile,
    Exchange,
    ExecutionMode,
    InstrumentKind,
    ModeId,
    ReviewSlotId,
    TradeState,
)
from trading.domain.ids import SequentialIdFactory
from trading.domain.primitives import Currency, Money, Price, TickSize
from trading.identification import (
    allowed_families_for,
    bind_debit_spread,
    bind_long_option,
    build_market_state,
    load_identification_policy,
    observe_p1_features,
    publish_macro_assessment,
    route_nifty_options,
    top_book_size,
)
from trading.identification.calendar import get_calendar_port
from trading.news.config import load_news_config
from trading.news.sources import NewsCollector
from trading.portfolio.risk_journal import (
    PortfolioRiskJournal,
    build_portfolio_risk_record,
)
from trading.risk.mode_ledger import FourModeBook
from trading.runtime.candidates import (
    build_future_snapshot,
    build_option_candidates,
    front_month_future,
)
from trading.runtime.cas_event_path import (
    ORACLE_MEASURED,
    CasEventDrivenConfig,
    M1ProviderEvent,
    active_m1_window,
    evaluate_m1_provider_event,
    measure_cas_entry_latency,
    record_episode_attempt,
)
from trading.runtime.cohort import experiment_id_for, persist_cohorts
from trading.runtime.event_risk import collect_event_risk
from trading.runtime.four_mode_producers import (
    build_four_mode_requests,
    produce_family_requests,
)
from trading.runtime.fyers_ws_monitor import FyersWsQuoteMonitor
from trading.runtime.isolation import assert_paper_isolation
from trading.runtime.m2_chain import following_week_epoch
from trading.runtime.notify import (
    format_eod_report,
    format_lifecycle_alert,
    format_post_trade,
    format_review_decision,
)
from trading.runtime.paper_runner import (
    PaperCycleResult,
    PaperRunner,
    PaperStrategyRequest,
)
from trading.runtime.protection import (
    ProtectionConfig,
    ProtectionCoordinator,
    build_protection_coordinator,
)
from trading.runtime.review_schedule import ReviewSlot, due_review_slots, parse_hhmm
from trading.runtime.session_heartbeat import write_session_heartbeat
from trading.runtime.session_routing import ProducedFamilyRequest, SessionRoutingProfile
from trading.runtime.startup_validation import validate_startup_configuration
from trading.storage.trading_store import TradingStore
from trading.strategies.macro import MacroAssessment
from trading.trade.exits import ExitEvaluation
from trading.trade.sentinel import StopSentinel

__all__ = [
    "PaperSessionConfig",
    "cas_paper_execute",
    "load_paper_session_config",
    "run_paper_session",
]

_IST = ZoneInfo("Asia/Kolkata")
_NOTIFY_MAX = 4000


class ReviewSlotConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: ReviewSlotId
    local: str


class PositionalReviewConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    nse_slots: tuple[ReviewSlotConfig, ...] = (
        ReviewSlotConfig(id=ReviewSlotId.NSE_MORNING, local="10:30"),
        ReviewSlotConfig(id=ReviewSlotId.NSE_AFTERNOON, local="14:30"),
    )
    mcx_slots: tuple[ReviewSlotConfig, ...] = ()


class PaperSessionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    entry_profile: EntryProfile = EntryProfile.STRICT
    routing_profile: SessionRoutingProfile = SessionRoutingProfile.LEGACY
    poll_interval_seconds: int = Field(ge=1)
    eod_local: str
    option_strikes_each_side: int = Field(ge=0, le=10)
    experiment_prefix: str
    strategy_ids: tuple[str, ...] = ()
    strategy_stances: dict[str, ExecutionMode] = Field(default_factory=dict)
    mode_stances: dict[str, ExecutionMode] = Field(default_factory=dict)
    family_stances: dict[str, ExecutionMode] = Field(default_factory=dict)
    commodity_underlying: str
    commodity_exchange: str
    commodity_segment: str
    cohort_dir: str
    store_path: str
    broker_state_path: str
    portfolio_risk_dir: str = "data/paper/portfolio_risk"
    session_heartbeat_path: str = "data/paper/session_heartbeat.json"
    positional_review: PositionalReviewConfig = Field(
        default_factory=PositionalReviewConfig
    )
    protection: ProtectionConfig = Field(default_factory=ProtectionConfig)
    cas_event_driven: CasEventDrivenConfig = Field(default_factory=CasEventDrivenConfig)
    # P0 audit remediation: block new PAPER exposure while exits/recovery stay on.
    new_entries_enabled: bool = True


class SessionNotifier(Protocol):
    def send(self, text: str) -> bool:
        """Deliver one advisory message."""
        ...


CycleBuild = Callable[
    [datetime],
    tuple[tuple[PaperStrategyRequest, ...], dict[str, FeatureSnapshot]],
]


def load_paper_session_config(path: Path) -> PaperSessionConfig:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("paper session config must be a mapping")
    payload.pop("schema_version", None)
    return PaperSessionConfig.model_validate(payload)


def cas_paper_execute(
    *,
    stance: ExecutionMode,
    allowed: frozenset[str],
    candidates: Sequence[FeatureSnapshot],
) -> tuple[bool, ExecutionMode]:
    """PAPER-submit CAS only when stance, allow-table, and one option agree."""
    execute = (
        stance is ExecutionMode.PAPER
        and "cas_microstructure" in allowed
        and len(candidates) == 1
    )
    return execute, ExecutionMode.PAPER if execute else ExecutionMode.SHADOW


class PaperSession:
    """Long-running PAPER loop. Sleep and data fetch are injected for tests."""

    def __init__(
        self,
        *,
        runner: PaperRunner,
        clock: Clock,
        session_config: PaperSessionConfig,
        session_hours: tuple[dt_time, dt_time],
        timezone: ZoneInfo,
        notifier: SessionNotifier,
        request_builder: CycleBuild,
        observation_start: datetime,
        capital_limit: Money,
        risk_policy_version: str,
        fill_model_version: str,
        code_version: str,
        charges_verified: bool,
        cohort_dir: Path,
        broker_state_path: Path | None = None,
        risk_journal: PortfolioRiskJournal | None = None,
        risk_policy: RiskPolicyConfig | None = None,
        account_id: str = "",
        protection: ProtectionCoordinator | None = None,
        session_heartbeat_path: Path | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        discovery_config: DiscoveryConfig | None = None,
    ) -> None:
        self._runner = runner
        self._clock = clock
        self._config = session_config
        self._discovery_config = discovery_config
        if self._config.entry_profile is EntryProfile.DISCOVERY:
            self._profile_version = (
                discovery_config.profile_version
                if discovery_config is not None
                else "discovery-v1"
            )
        else:
            self._profile_version = "strict"
        self._open, self._close = session_hours
        self._zone = timezone
        self._notifier = notifier
        self._builder = request_builder
        self._observation_start = observation_start
        self._capital_limit = capital_limit
        self._risk_policy_version = risk_policy_version
        self._fill_model_version = fill_model_version
        self._code_version = code_version
        self._charges_verified = charges_verified
        self._cohort_dir = cohort_dir
        self._broker_state_path = broker_state_path
        self._risk_journal = risk_journal
        self._risk_policy = risk_policy
        self._account_id = account_id
        self._protection = protection
        self._session_heartbeat_path = session_heartbeat_path
        self._sleeper = sleeper
        self._results: list[PaperCycleResult] = []
        self._cycle_count = 0
        self._eod_sent = False
        self._sentinel = StopSentinel(on_exit=self._on_sentinel_exit)
        self._m1_ingress: object | None = None
        self._latest_market_state: MarketState | None = None

    @property
    def session_config(self) -> PaperSessionConfig:
        """Validated session configuration loaded for this process."""
        return self._config

    @property
    def runner(self) -> PaperRunner:
        """Underlying paper runner (exits, recovery, risk)."""
        return self._runner

    def attach_m1_ingress(self, ingress: object) -> None:
        """Register the production M1 provider-event ingress."""
        self._m1_ingress = ingress

    def on_provider_quote(
        self,
        symbol: str,
        quote: MarketQuote,
        *,
        receive_time: datetime | None = None,
        quote_time: datetime | None = None,
        disconnected: bool = False,
    ) -> PaperCycleResult | None:
        """Production entry point: provider quote → ``submit_m1_event``."""
        from trading.runtime.m1_event_ingress import M1EventIngress

        if self._m1_ingress is None:
            self._m1_ingress = M1EventIngress(session=self, now_utc=self._clock.now_utc)
        if not isinstance(self._m1_ingress, M1EventIngress):
            return None
        result = self._m1_ingress.on_quote(
            symbol,
            quote,
            receive_time=receive_time,
            disconnected=disconnected,
        )
        return result if isinstance(result, PaperCycleResult) else None

    @property
    def sentinel(self) -> StopSentinel:
        """Real-time event-driven stop sentinel."""
        return self._sentinel

    def _on_sentinel_exit(
        self,
        trade_id: str,
        evaluation: ExitEvaluation,
        trigger_price: Price,
        ts: datetime,
    ) -> None:
        self._runner.trade_manager.apply_exit_evaluation(trade_id, evaluation)
        self._runner.flush_lifecycle()
        self._notifier.send(
            f"[SENTINEL] Trade {trade_id} {evaluation.kind} exit fired at {trigger_price}"[
                :_NOTIFY_MAX
            ]
        )

    def sync_sentinel(self) -> int:
        """Register all open positions with the real-time stop sentinel."""
        registered = 0
        for position in self._runner.trade_manager.list_positions():
            if position.state is TradeState.OPEN:
                book = self._runner.open_book.get(position.trade_id)
                if book is not None:
                    intent_obj, _ = book
                    if self._sentinel.register_position(position, intent_obj):
                        registered += 1
        return registered

    def run(self, *, once: bool = False) -> int:
        """Poll until EOD. ``once`` runs a single tick then returns."""
        if self._config.entry_profile is EntryProfile.DISCOVERY:
            banner = "ENTRY PROFILE: DISCOVERY (temporary)"
        else:
            banner = "ENTRY PROFILE: STRICT"
        print(banner, flush=True)
        logging.getLogger(__name__).info("%s", banner)
        recovery = self._runner.recover_lifecycle()
        for alert in recovery.alerts:
            self._notifier.send(format_lifecycle_alert(alert)[:_NOTIFY_MAX])
        self.sync_sentinel()
        if self._protection is not None:

            def m1_handler(
                symbol: str, quote: MarketQuote, receive_time: datetime
            ) -> object | None:
                return self.on_provider_quote(
                    symbol,
                    quote,
                    receive_time=receive_time,
                )

            self._protection.set_m1_quote_handler(m1_handler)
            self._protection.refresh_subscriptions()
            self._protection.start()
        try:
            return self._run_loop(once=once)
        finally:
            if self._protection is not None:
                self._protection.stop()

    def _entry_requests(
        self, requests: tuple[PaperStrategyRequest, ...]
    ) -> tuple[PaperStrategyRequest, ...]:
        """Hold new entries while keeping reconciliation, reviews and exits live.

        ``execute=False`` still evaluates strategies and records intents, so the
        candidate funnel stays auditable, but Layer 2 never submits an entry.
        Exits, protection and recovery do not consult this flag.
        """
        if self._config.new_entries_enabled:
            return requests
        return tuple(replace(item, execute=False) for item in requests)

    def _run_loop(self, *, once: bool) -> int:
        while True:
            now = self._clock.now_utc()
            local = now.astimezone(self._zone)
            if self._past_eod(local.time()):
                self._runner.flush_lifecycle()
                self._persist_broker()
                self._send_eod(now)
                return 0
            in_window = self._open <= local.time() <= self._close
            if in_window or self._has_open_positions():
                self.tick()
                self.sync_sentinel()
            if self._protection is not None:
                self._protection.refresh_subscriptions()
                self._protection.tick()
            self._persist_broker()
            if once:
                return 0
            self._sleeper(float(self._config.poll_interval_seconds))

    def tick(self) -> PaperCycleResult | None:
        """One L1→L3→L2 cycle plus exits. Returns None when the builder is empty."""
        holder = getattr(self._builder, "m1_holder", None)
        if isinstance(holder, dict) and not holder.get("keep"):
            holder["event"] = None
        now = self._clock.now_utc()
        requests, snapshots = self._builder(now)
        latest = getattr(self._builder, "latest_market_state", None)
        if isinstance(latest, MarketState):
            self._latest_market_state = latest
        requests = self._entry_requests(requests)
        result: PaperCycleResult | None = None
        if requests:
            result = self._runner.run_cycle(requests)
            self._results.append(result)
            self._runner.persist_cycle_evidence(result, as_of=now)
            for outcome in result.outcomes:
                experiment_id = next(
                    (
                        item.experiment_id
                        for item in requests
                        if item.strategy_id == outcome.strategy_id
                    ),
                    requests[0].experiment_id,
                )
                for text in format_post_trade(outcome, experiment_id=experiment_id):
                    self._notifier.send(text[:_NOTIFY_MAX])
        if snapshots:
            self._runner.manage_exits(snapshots)
        self._record_portfolio_risk(snapshots)
        self._run_due_reviews(snapshots)
        self._run_due_m2_carry_gate(snapshots, now)
        self._cycle_count += 1
        self._write_session_heartbeat(
            now=now, result=result, had_requests=bool(requests)
        )
        return result

    def submit_m1_event(self, event: M1ProviderEvent) -> PaperCycleResult | None:
        """Run only M1 through the session builder from one provider event.

        The 60-second poll clears any pending event and does not submit M1.
        """
        holder = getattr(self._builder, "m1_holder", None)
        if not isinstance(holder, dict):
            return None
        holder["keep"] = True
        holder["event"] = event
        try:
            now = self._clock.now_utc()
            requests, snapshots = self._builder(now)
            m1 = tuple(
                item
                for item in self._entry_requests(requests)
                if item.forced_mode_id is ModeId.M1_CAS
            )
            result = self._runner.run_cycle(m1) if m1 else None
            if snapshots:
                self._runner.manage_exits(snapshots)
            return result
        finally:
            holder["event"] = None
            holder["keep"] = False

    def _write_session_heartbeat(
        self,
        *,
        now: datetime,
        result: PaperCycleResult | None,
        had_requests: bool,
    ) -> None:
        if self._session_heartbeat_path is None:
            return
        local = now.astimezone(self._zone)
        route = result.route_decision if result is not None else None
        write_session_heartbeat(
            self._session_heartbeat_path,
            {
                "timestamp": now.isoformat(),
                "local_time": local.isoformat(),
                "cycle_count": self._cycle_count,
                "had_requests": had_requests,
                "new_entries_enabled": self._config.new_entries_enabled,
                "entry_profile": self._config.entry_profile.value,
                "profile_version": self._profile_version,
                "system_state": result.system_state.value if result else None,
                "entries_blocked": result.entries_blocked if result else None,
                "route_winner": route.paper_winner if route is not None else None,
                "route_reasons": [code.value for code in route.reason_codes]
                if route is not None
                else [],
                "open_positions": self._runner.open_position_count(),
                "outcome_count": len(result.outcomes) if result else 0,
            },
        )

    def _record_portfolio_risk(self, snapshots: dict[str, FeatureSnapshot]) -> None:
        if self._risk_journal is None or self._risk_policy is None:
            return
        now = self._clock.now_utc()
        record = build_portfolio_risk_record(
            broker=self._runner.broker,
            account_id=self._account_id,
            positions=self._runner.trade_manager.list_positions(),
            open_book=self._runner.open_book,
            snapshots=snapshots,
            risk_policy=self._risk_policy,
            id_factory=self._runner.id_factory,
            as_of=now,
        )
        self._risk_journal.append(record)

    def _has_open_positions(self) -> bool:
        return any(
            position.state is not TradeState.CLOSED
            for position in self._runner.trade_manager.list_positions()
        )

    def _run_due_reviews(self, snapshots: dict[str, FeatureSnapshot]) -> None:
        """Fire missed or on-time NSE review slots. Continuous exits already ran."""
        now = self._clock.now_utc()
        local = now.astimezone(self._zone)
        eod = _parse_hhmm(self._config.eod_local)
        slots = _configured_slots(self._config.positional_review)
        recorded = self._runner.recorded_review_slots(local.date())
        for work in due_review_slots(
            now_local=local,
            session_open=self._open,
            eod=eod,
            slots=slots,
            recorded=recorded,
        ):
            result = self._runner.run_review_slot(
                work.slot,
                snapshots,
                session_date=local.date(),
                missed_slot_ids=work.missed_slot_ids,
                configured_slots=slots,
            )
            for decision in result.decisions:
                self._notifier.send(format_review_decision(decision)[:_NOTIFY_MAX])

    def _run_due_m2_carry_gate(
        self, snapshots: dict[str, FeatureSnapshot], now: datetime
    ) -> None:
        """Run the Mode 2 carry gate once per session after entry cutoff."""
        local = now.astimezone(self._zone)
        calendar = get_calendar_port()
        if local.time() < calendar.entry_cutoff:
            return
        modes = load_modes_config()
        m2_policy = modes.modes[ModeId.M2_DIRECTIONAL]
        reference_amount = (
            self._capital_limit.amount * m2_policy.capital_share
        ).quantize(Decimal("0.01"))
        reference_capital = Money(reference_amount, Currency.INR)
        self._runner.run_m2_carry_gate(
            snapshots,
            session_date=local.date(),
            market=self._latest_market_state,
            mode_reference_capital=reference_capital,
            event_blackout=False,
            portfolio_entries_blocked=False,
            mode_daily_loss_breached=False,
        )

    def _past_eod(self, local_time: dt_time) -> bool:
        hour, minute = (int(part) for part in self._config.eod_local.split(":", 1))
        return local_time >= dt_time(hour, minute)

    def _send_eod(self, now: datetime) -> None:
        if self._eod_sent:
            return
        open_count = sum(
            1
            for position in self._runner.trade_manager.list_positions()
            if position.state is TradeState.OPEN
        )
        text = format_eod_report(
            self._results,
            open_trade_count=open_count,
            charges_verified=self._charges_verified,
        )
        self._notifier.send(text[:_NOTIFY_MAX])
        persist_cohorts(
            tuple(self._results),
            output_dir=self._cohort_dir,
            prefix=self._config.experiment_prefix,
            as_of=now,
            observation_start=self._observation_start,
            capital_limit=self._capital_limit,
            risk_policy_version=self._risk_policy_version,
            fill_model_version=self._fill_model_version,
            code_version=self._code_version,
            feature_set_version="paper-session-v1",
        )
        self._eod_sent = True

    def _persist_broker(self) -> None:
        if self._broker_state_path is None:
            return
        self._broker_state_path.parent.mkdir(parents=True, exist_ok=True)
        self._broker_state_path.write_text(
            json.dumps(self._runner.broker.dump_state()),
            encoding="utf-8",
        )


class _TelegramNotifier:
    def __init__(self, token: str, chat_id: str) -> None:
        self._token = token
        self._chat_id = chat_id

    def send(self, text: str) -> bool:
        return send_telegram_message(text, token=self._token, chat_id=self._chat_id)


def run_paper_session(
    repo_root: Path,
    *,
    account_config_path: Path,
    skip_auth: bool = False,
    once: bool = False,
    clock: Clock | None = None,
    sleeper: Callable[[float], None] | None = None,
    notifier: SessionNotifier | None = None,
    request_builder: Callable[
        [datetime], tuple[tuple[PaperStrategyRequest, ...], dict[str, FeatureSnapshot]]
    ]
    | None = None,
) -> int:
    """CLI entry: isolate, authenticate, then run the PAPER loop."""
    clock = clock or WallClock()
    account = load_config(account_config_path, expect_environment=Environment.PAPER)
    evaluation = load_evaluation_config(repo_root / "config" / "evaluation.yaml")
    risk = load_risk_policy(repo_root / "config" / "risk.yaml")
    session_cfg = load_paper_session_config(repo_root / "config" / "paper_session.yaml")
    modes_path = repo_root / "config" / "modes.yaml"
    modes_config = load_modes_config(modes_path) if modes_path.is_file() else None
    discovery_path = repo_root / "config" / "discovery.yaml"
    session_cfg, startup_warnings = validate_startup_configuration(
        session_cfg,
        modes_config,
        enforce_g3_shadow=True,
        environment=account.config.environment,
        discovery_path=discovery_path,
    )
    for warning in startup_warnings:
        logging.getLogger(__name__).warning("Startup validation warning: %s", warning)
    logging.getLogger(__name__).warning(
        "Loaded mode stances after startup validation: %s",
        {key: mode.value for key, mode in session_cfg.mode_stances.items()},
    )
    pipeline_cfg = load_data_pipeline_config(
        repo_root / "config" / "data_pipeline.yaml"
    )
    paper_data = load_paper_data_requirements(repo_root / "config" / "paper_data.yaml")
    discovery_cfg: DiscoveryConfig | None = None
    if session_cfg.entry_profile is EntryProfile.DISCOVERY and discovery_path.is_file():
        discovery_cfg = load_discovery_config(discovery_path)

    store = TradingStore.open(repo_root / session_cfg.store_path, clock=clock)
    session_date = clock.now_utc().astimezone(ZoneInfo("Asia/Kolkata")).date()
    mode_book = FourModeBook.reconstruct_from_store(
        store, session_date, modes_config=modes_config, discovery_config=discovery_cfg
    )
    funds_equity = (
        mode_book.total_equity
        if discovery_cfg is not None
        else Money.of("700000", Currency.INR)
    )
    funds = BrokerFunds(
        account_id=account.config.account_id,
        as_of=clock.now_utc(),
        equity=funds_equity,
        margin_used=Money.zero(Currency.INR),
        margin_available=funds_equity,
    )
    ids = SequentialIdFactory(clock.now_utc())
    broker = PaperBroker.for_session(
        clock=clock,
        id_factory=ids,
        funds=funds,
        fill_model=evaluation.config.fill_model,
        future_margin_fraction=risk.config.paper_future_margin_fraction,
    )
    assert_paper_isolation(account.config.environment, ExecutionMode.PAPER, broker)
    state_path = repo_root / session_cfg.broker_state_path
    if state_path.is_file():
        broker.load_state(json.loads(state_path.read_text(encoding="utf-8")))
    if not skip_auth:
        auth_status = run_telegram_auth(repo_root)
        if auth_status != 0:
            return auth_status
    runner = PaperRunner(
        account_config=account,
        risk_policy=risk,
        store=store,
        broker=broker,
        clock=clock,
        id_factory=ids,
        fill_model=evaluation.config.fill_model,
        execution_mode=ExecutionMode.PAPER,
        paper_data_requirements=paper_data,
        discovery_config=discovery_cfg,
    )
    settings = FyersSettings.from_repo_root_with_cache(repo_root)
    if notifier is None:
        if not telegram_configured(
            settings.a2a_telegram_bot_token, settings.a2a_telegram_chat_id
        ):
            return 1
        notifier = _TelegramNotifier(
            settings.a2a_telegram_bot_token, settings.a2a_telegram_chat_id
        )
    for warning in startup_warnings:
        notifier.send(f"STARTUP WARNING: {warning}")
    if request_builder is None:
        if session_cfg.routing_profile is SessionRoutingProfile.FOUR_MODE:
            if modes_config is None:
                logging.getLogger(__name__).error(
                    "Four-mode routing requires config/modes.yaml"
                )
                return 1
            request_builder = _four_mode_request_builder(
                repo_root,
                clock=clock,
                session_cfg=session_cfg,
                modes_config=modes_config,
                pipeline_cfg=pipeline_cfg,
                settings=settings,
                paper_data=paper_data,
                mode_book=mode_book,
                discovery_config=discovery_cfg,
            )
        else:
            request_builder = _live_request_builder(
                repo_root,
                clock=clock,
                session_cfg=session_cfg,
                pipeline_cfg=pipeline_cfg,
                settings=settings,
                broker=broker,
                paper_data=paper_data,
            )
    protection: ProtectionCoordinator | None = None
    if session_cfg.protection.enabled:
        ws_monitor = None
        if session_cfg.protection.ws_enabled:
            ws_monitor = FyersWsQuoteMonitor(settings, clock, repo_root)
        protection = build_protection_coordinator(
            runner=runner,
            clock=clock,
            config=session_cfg.protection,
            repo_root=repo_root,
            ws=ws_monitor,
        )
    session = PaperSession(
        runner=runner,
        clock=clock,
        session_config=session_cfg,
        session_hours=(
            _parse_hhmm(pipeline_cfg.session.open_local),
            _parse_hhmm(pipeline_cfg.session.close_local),
        ),
        timezone=ZoneInfo(pipeline_cfg.session.timezone),
        notifier=notifier,
        request_builder=request_builder,
        observation_start=clock.now_utc(),
        capital_limit=funds.equity,
        risk_policy_version=risk.config.policy_version,
        fill_model_version=evaluation.config.fill_model.version,
        code_version=account.version,
        charges_verified=evaluation.config.fill_model.charges_per_lot.is_verified,
        cohort_dir=repo_root / session_cfg.cohort_dir,
        broker_state_path=state_path,
        risk_journal=PortfolioRiskJournal(repo_root / session_cfg.portfolio_risk_dir),
        risk_policy=risk.config,
        account_id=account.config.account_id,
        protection=protection,
        session_heartbeat_path=repo_root / session_cfg.session_heartbeat_path,
        sleeper=sleeper or time.sleep,
        discovery_config=discovery_cfg,
    )
    return session.run(once=once)


def _parse_hhmm(value: str) -> dt_time:
    return parse_hhmm(value)


def _configured_slots(config: PositionalReviewConfig) -> tuple[ReviewSlot, ...]:
    slots: list[ReviewSlot] = []
    for item in (*config.nse_slots, *config.mcx_slots):
        slots.append(ReviewSlot(slot_id=item.id, local=parse_hhmm(item.local)))
    return tuple(slots)


def _quote_from_capture(
    capture: RawMarketCapture, spec: InstrumentSpec
) -> MarketQuote | None:
    event = normalize_fyers_quotes(
        capture,
        symbol=spec.trading_symbol,
        normalization_version="1",
        raw_ref="paper://quotes",
    )
    quotes = event.payload.get("quotes", [])
    if not isinstance(quotes, list) or not quotes or not isinstance(quotes[0], dict):
        return None
    row = quotes[0]
    tick = TickSize.of(spec.tick_size)
    last = row.get("lp", row.get("ltp"))
    bid = row.get("bid")
    ask = row.get("ask")
    if last is None:
        return None
    bid_size, ask_size = observed_book_sizes(row)
    return MarketQuote(
        last=Price.snap(str(last), tick),
        bid=Price.snap(str(bid), tick) if bid else None,
        ask=Price.snap(str(ask), tick) if ask else None,
        volume=optional_int_qty(row.get("volume")),
        bid_size=bid_size,
        ask_size=ask_size,
    )


def _quotes_from_capture(
    capture: RawMarketCapture,
    specs: dict[str, InstrumentSpec],
) -> dict[str, MarketQuote]:
    quotes: dict[str, MarketQuote] = {}
    for symbol, spec in specs.items():
        quote = _quote_from_capture(capture, spec)
        if quote is not None and quote.bid is not None and quote.ask is not None:
            quotes[symbol] = quote
    return quotes


def _with_option_depth(
    candidates: tuple[FeatureSnapshot, ...],
    feed: FyersMarketFeed,
    paper_data: PaperDataRequirements,
) -> tuple[FeatureSnapshot, ...]:
    """Attach REST depth sizes to the highest-OI options. Missing stays missing."""
    cap = paper_data.windows.depth.max_symbols
    if cap <= 0 or not candidates:
        return candidates
    ranked = sorted(
        candidates,
        key=lambda item: (
            -(
                0
                if item.derivatives is None or item.derivatives.open_interest is None
                else item.derivatives.open_interest
            ),
            item.contract.symbol,
        ),
    )
    remaining = cap
    by_symbol = {item.contract.symbol: item for item in candidates}
    for item in ranked:
        if remaining <= 0:
            break
        if top_book_size(item) is not None:
            continue
        remaining -= 1
        try:
            capture = feed.fetch_depth(item.contract.symbol)
        except (FyersApiError, ValueError, OSError):
            continue
        event = normalize_fyers_depth(
            capture,
            symbol=item.contract.symbol,
            normalization_version="1",
            raw_ref=capture.capture_id,
        )
        bid_size, ask_size = depth_top_sizes(event)
        if bid_size is None or ask_size is None:
            continue
        quote = item.market.model_copy(
            update={"bid_size": bid_size, "ask_size": ask_size}
        )
        by_symbol[item.contract.symbol] = item.model_copy(update={"market": quote})
    return tuple(by_symbol[item.contract.symbol] for item in candidates)


def _quotes_for_symbols(
    capture: RawMarketCapture, symbols: tuple[str, ...]
) -> dict[str, MarketQuote]:
    """REST protection quotes when only trading symbols are known."""
    if not symbols:
        return {}
    event = normalize_fyers_quotes(
        capture,
        symbol=symbols[0],
        normalization_version="1",
        raw_ref="paper://protection",
    )
    rows = event.payload.get("quotes", [])
    if not isinstance(rows, list):
        return {}
    tick = TickSize.of(Decimal("0.05"))
    quotes: dict[str, MarketQuote] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = row.get("symbol")
        if symbol not in symbols:
            continue
        last = row.get("lp", row.get("ltp"))
        bid = row.get("bid")
        ask = row.get("ask")
        if last is None:
            continue
        quote = MarketQuote(
            last=Price.snap(str(last), tick),
            bid=Price.snap(str(bid), tick) if bid else None,
            ask=Price.snap(str(ask), tick) if ask else None,
        )
        if quote.bid is not None and quote.ask is not None:
            quotes[str(symbol)] = quote
    return quotes


def _live_request_builder(
    repo_root: Path,
    *,
    clock: Clock,
    session_cfg: PaperSessionConfig,
    pipeline_cfg: DataPipelineConfig,
    settings: FyersSettings,
    broker: PaperBroker,
    paper_data: PaperDataRequirements | None = None,
) -> Callable[
    [datetime], tuple[tuple[PaperStrategyRequest, ...], dict[str, FeatureSnapshot]]
]:
    pipeline = build_pipeline(repo_root)
    catalog = InstrumentSpecStore(
        repo_root / pipeline_cfg.storage.root / pipeline_cfg.reference.instrument_subdir
    )
    news_config = load_news_config(repo_root / "config" / "news.yaml")
    identification = load_identification_policy(
        repo_root / "config" / "identification.yaml"
    )
    snapshot_store = SnapshotStore(
        repo_root / pipeline_cfg.storage.root / pipeline_cfg.storage.snapshot_subdir
    )
    collector = NewsCollector(news_config)
    zone = ZoneInfo(pipeline_cfg.session.timezone)
    feed = FyersMarketFeed(
        settings,
        clock,
        strike_count=pipeline_cfg.fyers.option_chain_strike_count,
        chain_greeks=pipeline_cfg.fyers.chain_greeks,
        history_oi_flag=pipeline_cfg.fyers.history_oi_flag,
    )
    last_winner_at: datetime | None = None
    last_winner_regime: str | None = None

    def build(
        now: datetime,
    ) -> tuple[tuple[PaperStrategyRequest, ...], dict[str, FeatureSnapshot]]:
        nonlocal last_winner_at, last_winner_regime
        event_risk = collect_event_risk(collector, news_config, as_of=now)
        snapshots: dict[str, FeatureSnapshot] = {}
        requests: list[PaperStrategyRequest] = []
        instruments: dict[str, InstrumentSpec] = {}
        index_underlying: FeatureSnapshot | None = None
        option_candidates: tuple[FeatureSnapshot, ...] = ()
        bar_event: CanonicalMarketEvent | None = None
        macro: MacroAssessment | None = None
        for underlying_cfg in pipeline_cfg.underlyings:
            if underlying_cfg.instrument_kind is InstrumentKind.FUTURE:
                continue
            if underlying_cfg.symbol == identification.vix_symbol:
                continue
            result = pipeline.run_once(underlying_cfg, now=now)
            if result.snapshot is None:
                continue
            index_underlying = result.snapshot
            macro = publish_macro_assessment(result.macro_news_factor)
            snapshots[index_underlying.contract.symbol] = index_underlying
            chain = next(
                (
                    event
                    for event in result.events
                    if event.event_type == "OPTION_CHAIN_SNAPSHOT"
                ),
                None,
            )
            if chain is None:
                continue
            bar_event = next(
                (
                    event
                    for event in result.events
                    if event.event_type == "BAR_SNAPSHOT"
                    and event.payload.get("resolution") == "5"
                ),
                None,
            )
            option_candidates, option_specs = build_option_candidates(
                chain,
                catalog,
                underlying=index_underlying,
                as_of=now,
                zone=zone,
                strikes_each_side=session_cfg.option_strikes_each_side,
            )
            if option_specs:
                quote_capture = feed.fetch_quotes(tuple(sorted(option_specs)))
                observed_quotes = _quotes_from_capture(quote_capture, option_specs)
                option_candidates, option_specs = build_option_candidates(
                    chain,
                    catalog,
                    underlying=index_underlying,
                    as_of=now,
                    zone=zone,
                    strikes_each_side=session_cfg.option_strikes_each_side,
                    quotes=observed_quotes,
                )
            if paper_data is not None:
                option_candidates = _with_option_depth(
                    option_candidates, feed, paper_data
                )
            instruments.update(option_specs)
            for candidate in option_candidates:
                snapshots[candidate.contract.symbol] = candidate
        future_spec = front_month_future(
            catalog,
            underlying=session_cfg.commodity_underlying,
            exchange=Exchange[session_cfg.commodity_exchange],
            as_of=now,
            zone=zone,
        )
        future_candidates: tuple[FeatureSnapshot, ...] = ()
        if future_spec is not None and index_underlying is not None:
            capture = feed.fetch_quotes((future_spec.trading_symbol,))
            future_quote = _quote_from_capture(capture, future_spec)
            if future_quote is not None:
                future_snap = build_future_snapshot(
                    future_spec,
                    quote=future_quote,
                    as_of=now,
                    zone=zone,
                    quality=index_underlying.quality,
                    lineage=index_underlying.lineage,
                    times=index_underlying.times,
                    extra_features=index_underlying.features,
                    feature_set_version="commodity_futures_v1",
                )
                future_candidates = (future_snap,)
                instruments[future_spec.trading_symbol] = future_spec
                snapshots[future_snap.contract.symbol] = future_snap
        if index_underlying is None:
            return (), snapshots
        vix_history = _vix_history(
            snapshot_store,
            symbol=identification.vix_symbol,
            now=now,
        )
        market_state = build_market_state(
            bar_event,
            underlying=index_underlying,
            option_candidates=option_candidates,
            event_risk=event_risk,
            macro=macro,
            as_of=now,
            policy=identification,
            vix_history=vix_history,
        )
        p1 = (
            None
            if paper_data is None
            else observe_p1_features(
                option_candidates, requirements=paper_data, market=market_state
            )
        )
        long_binding = bind_long_option(
            option_candidates, market=market_state, policy=identification, p1=p1
        )
        debit_binding = bind_debit_spread(
            option_candidates, market=market_state, policy=identification, p1=p1
        )
        regime_changed = last_winner_regime not in {None, market_state.trend.value}
        cooldown_active = (
            last_winner_at is not None
            and not regime_changed
            and now
            < last_winner_at + timedelta(minutes=identification.router.cooldown_minutes)
        )
        correlated = any(
            position.contract.underlying == index_underlying.contract.underlying
            for position in broker.get_positions()
        )
        allowed = allowed_families_for(
            market_state, identification, p1=p1, paper_data=paper_data
        )
        route, opportunities = route_nifty_options(
            market_state,
            long_option=long_binding,
            debit_spread=debit_binding,
            policy=identification,
            existing_correlated_exposure=correlated,
            cooldown_active=cooldown_active,
            allowed_families=allowed,
            p1=p1,
        )
        if route.paper_winner is not None:
            last_winner_at = now
            last_winner_regime = market_state.trend.value
        by_strategy = {item.strategy_id: item for item in opportunities}
        for strategy_id in session_cfg.strategy_ids:
            configured_stance = session_cfg.strategy_stances.get(
                strategy_id, ExecutionMode.SUSPENDED
            )
            if configured_stance is ExecutionMode.SUSPENDED:
                continue
            if strategy_id in {"positional_long_option", "debit_spread"}:
                opportunity = by_strategy.get(strategy_id)
                binding = (
                    long_binding
                    if strategy_id == "positional_long_option"
                    else debit_binding
                )
                candidates = (
                    binding.candidates
                    if opportunity is None
                    else opportunity.bound.candidates
                )
                is_eligible = binding.binding.eligible and strategy_id in allowed
                execute = configured_stance is ExecutionMode.PAPER and is_eligible
                setup = None if opportunity is None else opportunity.setup_features
                mode = ExecutionMode.PAPER if execute else ExecutionMode.SHADOW
            elif strategy_id == "commodity_futures_trend":
                candidates = future_candidates
                execute = False
                setup = None
                mode = ExecutionMode.SHADOW
            elif strategy_id == "cas_microstructure":
                candidates = long_binding.candidates
                execute, mode = cas_paper_execute(
                    stance=configured_stance,
                    allowed=allowed,
                    candidates=candidates,
                )
                setup = long_binding.setup_features if execute else None
            else:
                candidates = ()
                execute = False
                setup = None
                mode = ExecutionMode.SHADOW
            requests.append(
                PaperStrategyRequest(
                    strategy_id=strategy_id,
                    underlying=index_underlying,
                    candidates=candidates,
                    instruments=instruments,
                    event_risk_state=event_risk,
                    experiment_id=experiment_id_for(
                        session_cfg.experiment_prefix, strategy_id, now
                    ),
                    execution_mode=mode,
                    macro=macro,
                    execute=execute,
                    setup_features=setup,
                    route_decision=route,
                )
            )
        return tuple(requests), snapshots

    return build


def _four_mode_request_builder(
    repo_root: Path,
    *,
    clock: Clock,
    session_cfg: PaperSessionConfig,
    modes_config: ModesConfig,
    pipeline_cfg: DataPipelineConfig,
    settings: FyersSettings,
    paper_data: PaperDataRequirements | None = None,
    mode_book: FourModeBook | None = None,
    discovery_config: DiscoveryConfig | None = None,
) -> Callable[
    [datetime], tuple[tuple[PaperStrategyRequest, ...], dict[str, FeatureSnapshot]]
]:
    """Four-mode producers: independent family bindings, no legacy one-winner router."""
    pipeline = build_pipeline(repo_root)
    catalog = InstrumentSpecStore(
        repo_root / pipeline_cfg.storage.root / pipeline_cfg.reference.instrument_subdir
    )
    news_config = load_news_config(repo_root / "config" / "news.yaml")
    identification = load_identification_policy(
        repo_root / "config" / "identification.yaml"
    )
    snapshot_store = SnapshotStore(
        repo_root / pipeline_cfg.storage.root / pipeline_cfg.storage.snapshot_subdir
    )
    collector = NewsCollector(news_config)
    zone = ZoneInfo(pipeline_cfg.session.timezone)
    feed = FyersMarketFeed(
        settings,
        clock,
        strike_count=pipeline_cfg.fyers.option_chain_strike_count,
        chain_greeks=pipeline_cfg.fyers.chain_greeks,
        history_oi_flag=pipeline_cfg.fyers.history_oi_flag,
    )
    cas_latency_samples: list[int] = []
    cas_quote_ages: list[int] = []
    cas_execution_latencies: list[int] = []
    cas_exit_gaps: list[int] = []
    cas_attempts_today = 0
    cas_session_date: date | None = None
    m1_holder: dict[str, object] = {"event": None, "keep": False}

    def build(
        now: datetime,
    ) -> tuple[tuple[PaperStrategyRequest, ...], dict[str, FeatureSnapshot]]:
        nonlocal cas_attempts_today, cas_session_date
        session_day = now.astimezone(_IST).date()
        if cas_session_date != session_day:
            cas_session_date = session_day
            cas_attempts_today = 0
        event_risk = collect_event_risk(collector, news_config, as_of=now)
        snapshots: dict[str, FeatureSnapshot] = {}
        instruments: dict[str, InstrumentSpec] = {}
        index_underlying: FeatureSnapshot | None = None
        option_candidates: tuple[FeatureSnapshot, ...] = ()
        bar_event: CanonicalMarketEvent | None = None
        macro: MacroAssessment | None = None
        for underlying_cfg in pipeline_cfg.underlyings:
            if underlying_cfg.instrument_kind is InstrumentKind.FUTURE:
                continue
            if underlying_cfg.symbol == identification.vix_symbol:
                continue
            result = pipeline.run_once(underlying_cfg, now=now)
            if result.snapshot is None:
                continue
            index_underlying = result.snapshot
            macro = publish_macro_assessment(result.macro_news_factor)
            snapshots[index_underlying.contract.symbol] = index_underlying
            chain = next(
                (
                    event
                    for event in result.events
                    if event.event_type == "OPTION_CHAIN_SNAPSHOT"
                ),
                None,
            )
            if chain is None:
                continue
            bar_event = next(
                (
                    event
                    for event in result.events
                    if event.event_type == "BAR_SNAPSHOT"
                    and event.payload.get("resolution") == "5"
                ),
                None,
            )
            option_candidates, option_specs = build_option_candidates(
                chain,
                catalog,
                underlying=index_underlying,
                as_of=now,
                zone=zone,
                strikes_each_side=session_cfg.option_strikes_each_side,
            )
            if option_specs:
                quote_capture = feed.fetch_quotes(tuple(sorted(option_specs)))
                observed_quotes = _quotes_from_capture(quote_capture, option_specs)
                option_candidates, option_specs = build_option_candidates(
                    chain,
                    catalog,
                    underlying=index_underlying,
                    as_of=now,
                    zone=zone,
                    strikes_each_side=session_cfg.option_strikes_each_side,
                    quotes=observed_quotes,
                )
            if paper_data is not None:
                option_candidates = _with_option_depth(
                    option_candidates, feed, paper_data
                )
            option_candidates, option_specs = _merge_following_week_chain(
                option_candidates,
                option_specs,
                chain=chain,
                catalog=catalog,
                underlying=index_underlying,
                as_of=now,
                zone=zone,
                feed=feed,
                pipeline_symbol=underlying_cfg.symbol,
            )
            instruments.update(option_specs)
            for candidate in option_candidates:
                snapshots[candidate.contract.symbol] = candidate
        if index_underlying is None:
            return (), snapshots
        vix_history = _vix_history(
            snapshot_store,
            symbol=identification.vix_symbol,
            now=now,
        )
        market_state = build_market_state(
            bar_event,
            underlying=index_underlying,
            option_candidates=option_candidates,
            event_risk=event_risk,
            macro=macro,
            as_of=now,
            policy=identification,
            vix_history=vix_history,
        )
        p1 = (
            None
            if paper_data is None
            else observe_p1_features(
                option_candidates, requirements=paper_data, market=market_state
            )
        )
        master_symbols = frozenset(instruments)
        produced = produce_family_requests(
            modes_config=modes_config,
            mode_stances=session_cfg.mode_stances,
            family_stances=session_cfg.family_stances,
            candidates=option_candidates,
            market=market_state,
            policy=identification,
            p1=p1,
            master_symbols=master_symbols,
        )
        event = m1_holder.get("event")
        window = active_m1_window(now, session_cfg.cas_event_driven)
        in_window = window is not None
        quote_ages = tuple(cas_quote_ages)
        execution_latencies = tuple(cas_execution_latencies)
        exit_gaps = tuple(cas_exit_gaps)
        provenance = "unlabelled"
        if isinstance(event, M1ProviderEvent) and event.provenance == ORACLE_MEASURED:
            provenance = ORACLE_MEASURED
        latency_report = measure_cas_entry_latency(
            tuple(cas_latency_samples),
            config=session_cfg.cas_event_driven,
            provenance=provenance,
            quote_ages_ms=quote_ages,
            execution_latencies_ms=execution_latencies,
            exit_monitor_gaps_ms=exit_gaps,
        )
        adjusted: list[ProducedFamilyRequest] = []
        for item in produced:
            if item.spec.mode_id is not ModeId.M1_CAS:
                adjusted.append(item)
                continue
            m1_stance = session_cfg.mode_stances.get(
                ModeId.M1_CAS.value, ExecutionMode.SHADOW
            )
            if mode_book is not None:
                m1_cap = mode_book.get_ledger(ModeId.M1_CAS).allocated_capital.amount
            elif discovery_config is not None:
                m1_cap = discovery_config.books.starting_equity_per_mode
            else:
                m1_cap = (
                    modes_config.modes[ModeId.M1_CAS].capital_share
                    * Decimal("700000")
                )
            gate = evaluate_m1_provider_event(
                item=item,
                event=event if isinstance(event, M1ProviderEvent) else None,
                option_candidates=option_candidates,
                config=session_cfg.cas_event_driven,
                stance=m1_stance,
                attempts_today=cas_attempts_today,
                in_window=in_window,
                latency_report=latency_report,
                session_date=session_day.isoformat(),
                episode_ledger=repo_root / "data" / "paper" / "m1_episodes.json",
                mode_capital=m1_cap,
            )
            if gate.latency_limitation:
                logging.getLogger(__name__).warning(
                    "M1 latency limitation (PAPER continues): %s",
                    gate.latency_limitation,
                )
            episode_id = ""
            if isinstance(event, M1ProviderEvent):
                episode_id = event.episode_id or item.spec.family_id.value
            execute = gate.execute
            mode = gate.execution_mode
            if execute and isinstance(event, M1ProviderEvent):
                cas_attempts_today += 1
                record_episode_attempt(
                    repo_root / "data" / "paper" / "m1_episodes.json",
                    session_date=session_day.isoformat(),
                    episode_id=episode_id,
                )
                if event.provenance == ORACLE_MEASURED and event.event_time is not None:
                    cas_latency_samples.append(
                        max(
                            0,
                            int(
                                (event.decided_at - event.receive_time).total_seconds()
                                * 1000
                            ),
                        )
                    )
                    if event.quote_time is not None and event.submitted_at is not None:
                        cas_quote_ages.append(
                            max(
                                0,
                                int(
                                    (
                                        event.submitted_at - event.quote_time
                                    ).total_seconds()
                                    * 1000
                                ),
                            )
                        )
                    if (
                        event.execution_completed_at is not None
                        and event.submitted_at is not None
                    ):
                        cas_execution_latencies.append(
                            max(
                                0,
                                int(
                                    (
                                        event.execution_completed_at - event.submitted_at
                                    ).total_seconds()
                                    * 1000
                                ),
                            )
                        )
                    if (
                        event.exit_quote_at is not None
                        and event.previous_exit_quote_at is not None
                    ):
                        cas_exit_gaps.append(
                            max(
                                0,
                                int(
                                    (
                                        event.exit_quote_at - event.previous_exit_quote_at
                                    ).total_seconds()
                                    * 1000
                                ),
                            )
                        )
            adjusted.append(
                item.__class__(
                    spec=item.spec,
                    bound=item.bound,
                    execute=execute,
                    execution_mode=mode if execute else ExecutionMode.SHADOW,
                )
            )
        requests = build_four_mode_requests(
            adjusted,
            index_underlying=index_underlying,
            instruments=instruments,
            event_risk=event_risk,
            macro=macro,
            experiment_prefix=session_cfg.experiment_prefix,
            now=now,
        )
        build.latest_market_state = market_state  # type: ignore[attr-defined]
        return requests, snapshots

    build.m1_holder = m1_holder  # type: ignore[attr-defined]
    build.latest_market_state = None  # type: ignore[attr-defined]
    return build


def _vix_history(
    store: SnapshotStore, *, symbol: str, now: datetime
) -> tuple[Decimal, ...]:
    """Daily India VIX closes from snapshot store (fail closed on empty)."""
    records = store.read(
        symbol=symbol,
        start=now - timedelta(days=45),
        end=now,
    )
    daily: dict[date, Decimal] = {}
    for record in records:
        if record.snapshot is None:
            continue
        snap = record.snapshot
        level = snap.features.get("india_vix")
        if level is None or level <= 0:
            last = snap.market.last
            close = snap.market.close
            price = last if last is not None else close
            level = None if price is None else price.value
        if level is None or level <= 0:
            continue
        daily[record.as_of.astimezone(_IST).date()] = Decimal(level)
    return tuple(daily[key] for key in sorted(daily))


_FOLLOWING_WEEK_STRIKES = 8


def _merge_following_week_chain(
    option_candidates: tuple[FeatureSnapshot, ...],
    option_specs: dict[str, InstrumentSpec],
    *,
    chain: CanonicalMarketEvent,
    catalog: InstrumentSpecStore,
    underlying: FeatureSnapshot,
    as_of: datetime,
    zone: ZoneInfo,
    feed: FyersMarketFeed,
    pipeline_symbol: str,
) -> tuple[tuple[FeatureSnapshot, ...], dict[str, InstrumentSpec]]:
    """Add the following-week chain when the loaded chain is a nearer expiry."""
    already = {
        item.contract.expiry
        for item in option_candidates
        if item.contract.expiry is not None
    }
    epoch = following_week_epoch(
        chain,
        as_of=as_of.astimezone(zone).date(),
        calendar=get_calendar_port(),
        already_listed=already,
    )
    if epoch is None:
        return option_candidates, option_specs
    try:
        capture = feed.fetch_option_chain(pipeline_symbol, expiry_epoch=epoch)
    except (FyersApiError, OSError):
        logging.getLogger(__name__).warning(
            "following-week chain fetch failed; M2 will abstain if that expiry is absent"
        )
        return option_candidates, option_specs
    event = normalize_fyers_option_chain(
        capture,
        symbol=pipeline_symbol,
        normalization_version="1",
        raw_ref="m2-following-week",
    )
    extra, extra_specs = build_option_candidates(
        event,
        catalog,
        underlying=underlying,
        as_of=as_of,
        zone=zone,
        strikes_each_side=_FOLLOWING_WEEK_STRIKES,
    )
    marked: list[FeatureSnapshot] = []
    for snap in extra:
        features = dict(snap.features)
        features["following_week_chain"] = Decimal(1)
        marked.append(snap.model_copy(update={"features": features}))
    merged_specs = dict(option_specs)
    merged_specs.update(extra_specs)
    return option_candidates + tuple(marked), merged_specs
