"""Unattended PAPER session: Telegram auth, then L1→L3→L2 against the paper broker."""

from __future__ import annotations

import json
import time
from collections import deque
from collections.abc import Callable, Sequence
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
from trading.config.paper_data import load_paper_data_requirements
from trading.config.schema import Environment
from trading.data.config import DataPipelineConfig, load_data_pipeline_config
from trading.data.events import CanonicalMarketEvent, RawMarketCapture
from trading.data.fyers.auth import run_telegram_auth
from trading.data.fyers.client import FyersApiError, FyersMarketFeed
from trading.data.fyers.telegram import send_telegram_message, telegram_configured
from trading.data.normalize import normalize_fyers_depth, normalize_fyers_quotes
from trading.data.pipeline import build_pipeline
from trading.data.prices import depth_top_sizes, observed_book_sizes, optional_int_qty
from trading.data.settings import FyersSettings
from trading.data.storage.instrument_store import InstrumentSpecStore
from trading.data.storage.snapshot_store import SnapshotStore
from trading.domain.clock import Clock, WallClock
from trading.domain.contracts import FeatureSnapshot, InstrumentSpec
from trading.domain.contracts.paper_data import PaperDataRequirements
from trading.domain.contracts.snapshot import MarketQuote
from trading.domain.enums import (
    Exchange,
    ExecutionMode,
    InstrumentKind,
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
from trading.news.config import load_news_config
from trading.news.sources import NewsCollector
from trading.runtime.candidates import (
    build_future_snapshot,
    build_option_candidates,
    front_month_future,
)
from trading.runtime.cohort import experiment_id_for, persist_cohorts
from trading.runtime.event_risk import collect_event_risk
from trading.runtime.isolation import assert_paper_isolation
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
from trading.runtime.review_schedule import ReviewSlot, due_review_slots, parse_hhmm
from trading.storage.trading_store import TradingStore
from trading.strategies.macro import MacroAssessment

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

    poll_interval_seconds: int = Field(ge=1)
    eod_local: str
    option_strikes_each_side: int = Field(ge=0, le=10)
    experiment_prefix: str
    strategy_ids: tuple[str, ...]
    strategy_stances: dict[str, ExecutionMode]
    commodity_underlying: str
    commodity_exchange: str
    commodity_segment: str
    cohort_dir: str
    store_path: str
    broker_state_path: str
    positional_review: PositionalReviewConfig = Field(
        default_factory=PositionalReviewConfig
    )


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
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._runner = runner
        self._clock = clock
        self._config = session_config
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
        self._sleeper = sleeper
        self._results: list[PaperCycleResult] = []
        self._eod_sent = False

    def run(self, *, once: bool = False) -> int:
        """Poll until EOD. ``once`` runs a single tick then returns."""
        recovery = self._runner.recover_lifecycle()
        for alert in recovery.alerts:
            self._notifier.send(format_lifecycle_alert(alert)[:_NOTIFY_MAX])
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
            self._persist_broker()
            if once:
                return 0
            self._sleeper(float(self._config.poll_interval_seconds))

    def tick(self) -> PaperCycleResult | None:
        """One L1→L3→L2 cycle plus exits. Returns None when the builder is empty."""
        now = self._clock.now_utc()
        requests, snapshots = self._builder(now)
        result: PaperCycleResult | None = None
        if requests:
            result = self._runner.run_cycle(requests)
            self._results.append(result)
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
        self._run_due_reviews(snapshots)
        return result

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
        for slot in due_review_slots(
            now_local=local,
            session_open=self._open,
            eod=eod,
            slots=slots,
            recorded=recorded,
        ):
            result = self._runner.run_review_slot(
                slot, snapshots, session_date=local.date()
            )
            for decision in result.decisions:
                self._notifier.send(format_review_decision(decision)[:_NOTIFY_MAX])

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
    pipeline_cfg = load_data_pipeline_config(
        repo_root / "config" / "data_pipeline.yaml"
    )
    paper_data = load_paper_data_requirements(repo_root / "config" / "paper_data.yaml")
    funds = BrokerFunds(
        account_id=account.config.account_id,
        as_of=clock.now_utc(),
        equity=Money.of("700000", Currency.INR),
        margin_used=Money.zero(Currency.INR),
        margin_available=Money.of("700000", Currency.INR),
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
    store = TradingStore.open(repo_root / session_cfg.store_path, clock=clock)
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
    )
    settings = FyersSettings.from_repo_root(repo_root)
    if notifier is None:
        if not telegram_configured(
            settings.a2a_telegram_bot_token, settings.a2a_telegram_chat_id
        ):
            return 1
        notifier = _TelegramNotifier(
            settings.a2a_telegram_bot_token, settings.a2a_telegram_chat_id
        )
    if request_builder is None:
        request_builder = _live_request_builder(
            repo_root,
            clock=clock,
            session_cfg=session_cfg,
            pipeline_cfg=pipeline_cfg,
            settings=settings,
            broker=broker,
            paper_data=paper_data,
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
        sleeper=sleeper or time.sleep,
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


def _live_request_builder(  # noqa: PLR0915 - point-in-time episode composition
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

    def build(  # noqa: PLR0912, PLR0915 - fail-closed orchestration branches
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
                execute = (
                    configured_stance is ExecutionMode.PAPER
                    and opportunity is not None
                    and opportunity.execution
                )
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
