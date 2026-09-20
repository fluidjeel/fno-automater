"""Command-line entry points for operations and the data pipeline."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from trading.ai.advise import run_advise_agent
from trading.ai.history_ports import (
    SnapshotMarketPort,
    StaticNewsPort,
    history_evidence,
)
from trading.ai.llm_settings import LlmSettings, load_llm_settings
from trading.ai.loop import run_weekly_agent
from trading.ai.openai_compat import OpenAICompatLlm
from trading.ai.ports import LlmTimeoutError, LlmTurn
from trading.ai.recording import RecordingLlm, persist_agent_run
from trading.ai.tools import ToolContext
from trading.analytics.eligibility import evaluate_eligibility
from trading.analytics.improvements import apply_stale_status, cluster_improvements
from trading.analytics.judgment import JudgmentError, evaluate_judgment
from trading.analytics.scorecard import EvaluationError, build_scorecard
from trading.config import (
    AgentConfigError,
    EvaluationConfigError,
    LoadedEvaluationConfig,
    load_agent_config,
    load_config,
    load_evaluation_config,
)
from trading.config.schema import Environment
from trading.data.backfill import backfill_history, backfill_instruments
from trading.data.config import load_data_pipeline_config
from trading.data.fyers.auth import run_interactive_auth, run_refresh, run_telegram_auth
from trading.data.fyers.client import FyersApiError, FyersMarketFeed
from trading.data.fyers.telegram import send_telegram_message, telegram_configured
from trading.data.fyers.ws import FyersTickStream
from trading.data.macro_news import format_macro_summary, load_macro_news_jsonl
from trading.data.normalize import normalize_fyers_history
from trading.data.pipeline import build_pipeline
from trading.data.replay import build_replay_engine
from trading.data.settings import FyersSettings
from trading.data.storage.catalog import CatalogWriter
from trading.data.storage.parquet_store import JsonlEventStore
from trading.domain.clock import WallClock, ensure_utc
from trading.domain.contracts import CohortPackage
from trading.domain.ids import SequentialIdFactory
from trading.news.cluster import cluster_news
from trading.news.config import NewsSubsystemConfig, load_news_config
from trading.news.contracts import NewsSourceTier, SentimentSnapshot
from trading.news.proposal import abstaining_proposal
from trading.news.scoring import build_snapshot
from trading.news.sentiment import (
    FinBertClassifier,
    SentimentClassifier,
    UnavailableSentimentClassifier,
)
from trading.news.sources import NewsCollector, normalize_news_item
from trading.news.storage import NewsJsonlStore
from trading.ops.attention import (
    AttentionSink,
    scan_attention_blockers,
    telegram_attention_sink,
)
from trading.runtime.paper_session import load_paper_session_config, run_paper_session
from trading.runtime.watchdog import run_paper_watchdog
from trading.storage.trading_store import TradingStore

__all__ = ["main"]


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def _cmd_auth_fyers(args: argparse.Namespace) -> int:
    return run_interactive_auth(
        _repo_root(),
        auth_code=args.auth_code,
        open_browser=not args.no_browser,
    )


def _cmd_auth_refresh(args: argparse.Namespace) -> int:
    return run_refresh(_repo_root())


def _cmd_auth_telegram(args: argparse.Namespace) -> int:
    return run_telegram_auth(_repo_root())


def _macro_news_path(root: Path, file_arg: str) -> Path:
    if file_arg:
        path = Path(file_arg)
        return path if path.is_absolute() else root / path
    config = load_data_pipeline_config(root / "config" / "data_pipeline.yaml")
    return root / config.macro_news.input_file


def _cmd_data_news_validate(args: argparse.Namespace) -> int:
    root = _repo_root()
    path = _macro_news_path(root, args.file)
    print(f"file: {path}")
    if not path.is_file():
        print("status: missing (no macro-news factor will be applied)")
        return 0
    loaded = load_macro_news_jsonl(path)
    print(f"valid records: {len(loaded.items)}")
    for duplicate in loaded.skipped_duplicates:
        print(f"  WARN duplicate event_id skipped: {duplicate}", file=sys.stderr)
    for error in loaded.errors:
        print(f"  ERROR {error}", file=sys.stderr)
    if loaded.errors:
        return 1
    return 0


def _cmd_data_fetch(args: argparse.Namespace) -> int:
    root = _repo_root()
    pipeline = build_pipeline(root)
    config = load_data_pipeline_config(root / "config" / "data_pipeline.yaml")
    targets = [
        u for u in config.underlyings if not args.symbol or u.symbol == args.symbol
    ]
    if not targets:
        print(f"no underlying matching {args.symbol!r}", file=sys.stderr)
        return 1
    for underlying in targets:
        result = pipeline.run_once(underlying)
        status = "snapshot" if result.snapshot else "no-snapshot"
        macro = format_macro_summary(result.macro_news_factor)
        event_types = ",".join(event.event_type for event in result.events)
        print(
            f"{result.symbol}: {status} events={event_types} "
            f"{macro} raw_refs={len(result.raw_refs)}"
        )
        for error in result.macro_news_parse_errors:
            print(f"  WARN macro-news {error}", file=sys.stderr)
    return 0


def _cmd_data_stream(args: argparse.Namespace) -> int:
    root = _repo_root()
    config = load_data_pipeline_config(root / "config" / "data_pipeline.yaml")
    settings = FyersSettings.from_repo_root(root)
    cached = settings.load_cached_token(root)
    if cached and not settings.fyers_access_token:
        settings = settings.model_copy(update={"fyers_access_token": cached})
    targets = [
        u for u in config.underlyings if not args.symbol or u.symbol == args.symbol
    ]
    if not targets:
        print(f"no underlying matching {args.symbol!r}", file=sys.stderr)
        return 1
    store = JsonlEventStore(root / config.storage.root)
    clock = WallClock()
    stream = FyersTickStream(
        settings,
        clock,
        store,
        repo_root=root,
        normalization_version=config.normalization_version,
        channel=config.fyers.ws_channel,
        reconnect=config.fyers.ws_reconnect,
        reconnect_attempts=config.fyers.ws_reconnect_attempts,
        reconnect_backoff_seconds=config.fyers.ws_reconnect_backoff_seconds,
    )
    tick_max_age = config.freshness.get("tick_max_age_ms", {}).get("5m", 5_000)
    for underlying in targets:
        if args.daemon:
            result = stream.run_daemon(
                underlying.symbol,
                tick_max_age_ms=tick_max_age,
            )
        else:
            max_ticks = args.max_ticks or config.fyers.ws_max_ticks
            duration = args.duration or config.fyers.ws_duration_seconds
            result = stream.collect(
                underlying.symbol,
                max_ticks=max_ticks,
                duration_seconds=duration,
                tick_max_age_ms=tick_max_age,
            )
        print(
            f"{result.symbol}: {len(result.ticks)} tick(s) "
            f"stopped={result.stopped_reason} reconnects={result.reconnects}"
        )
    return 0


def _fyers_feed(root: Path) -> FyersMarketFeed:
    config = load_data_pipeline_config(root / "config" / "data_pipeline.yaml")
    settings = FyersSettings.from_repo_root(root)
    cached = settings.load_cached_token(root)
    if cached and not settings.fyers_access_token:
        settings = settings.model_copy(update={"fyers_access_token": cached})
    return FyersMarketFeed(
        settings,
        WallClock(),
        strike_count=config.fyers.option_chain_strike_count,
        chain_greeks=config.fyers.chain_greeks,
        history_oi_flag=config.fyers.history_oi_flag,
    )


def _cmd_data_backfill_instruments(_args: argparse.Namespace) -> int:
    root = _repo_root()
    config = load_data_pipeline_config(root / "config" / "data_pipeline.yaml")
    store = JsonlEventStore(root / config.storage.root)
    results = backfill_instruments(
        pipeline_config=config,
        repo_root=root,
        store=store,
        clock=WallClock(),
    )
    for result in results:
        print(f"{result.segment}: {result.spec_count} spec(s) raw={result.raw_ref}")
    if not results:
        print("no reference.segments configured", file=sys.stderr)
        return 1
    return 0


def _cmd_data_backfill_history(args: argparse.Namespace) -> int:
    root = _repo_root()
    config = load_data_pipeline_config(root / "config" / "data_pipeline.yaml")
    targets = [
        underlying
        for underlying in config.underlyings
        if not args.symbol or underlying.symbol == args.symbol
    ]
    if not targets:
        print(f"no underlying matching {args.symbol!r}", file=sys.stderr)
        return 1
    if args.resolutions:
        resolutions = tuple(
            item.strip() for item in args.resolutions.split(",") if item.strip()
        )
    else:
        resolutions = config.fyers.bar_resolutions
    days = args.days if args.days > 0 else config.fyers.bar_lookback_days
    store = JsonlEventStore(root / config.storage.root)
    catalog = CatalogWriter(
        root / config.storage.root,
        duckdb_path=root / config.storage.duckdb_path,
        parquet_subdir=config.storage.parquet_subdir,
    )
    feed = _fyers_feed(root)
    clock = WallClock()
    for underlying in targets:
        results = backfill_history(
            pipeline_config=config,
            repo_root=root,
            feed=feed,
            store=store,
            clock=clock,
            underlying=underlying,
            resolutions=resolutions,
            days=days,
            catalog=catalog,
        )
        for result in results:
            print(
                f"{result.symbol} res={result.resolution} "
                f"windows={len(result.windows)} events={len(result.event_ids)}"
            )
    return 0


def _cmd_data_replay(args: argparse.Namespace) -> int:
    root = _repo_root()
    config = load_data_pipeline_config(root / "config" / "data_pipeline.yaml")
    engine = build_replay_engine(root)
    end = WallClock().now_utc()
    start = end - timedelta(hours=args.hours)
    for underlying in config.underlyings:
        if args.symbol and underlying.symbol != args.symbol:
            continue
        result = engine.replay(underlying, start=start, end=end)
        print(f"{result.symbol}: {len(result.snapshots)} snapshot(s) replayed")
    return 0


def _news_paths(
    root: Path, args: argparse.Namespace
) -> tuple[NewsSubsystemConfig, NewsJsonlStore]:
    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = root / config_path
    store_path = Path(args.store)
    if not store_path.is_absolute():
        store_path = root / store_path
    return load_news_config(config_path), NewsJsonlStore(store_path)


def _news_classifier(config: NewsSubsystemConfig) -> SentimentClassifier:
    if config.sentiment_provider == "finbert":
        return FinBertClassifier(
            config.finbert_model_path,
            batch_size=config.max_classifier_batch_size,
            max_tokens=config.max_classifier_tokens,
        )
    return UnavailableSentimentClassifier()


def _cmd_news(args: argparse.Namespace) -> int:
    root = _repo_root()
    config, store = _news_paths(root, args)
    if args.news_cmd == "status":
        for name in ("items", "events", "snapshots", "risks", "proposals"):
            print(f"{name}: {store.count(name)}")
        cycle = store.latest_cycle()
        if cycle is not None:
            print(f"last_cycle_at: {cycle.get('as_of')}")
            health = cycle.get("source_health", [])
            if isinstance(health, list):
                for source in health:
                    if not isinstance(source, dict):
                        continue
                    print(f"{source.get('source_id')}: {source.get('status')}")
        return 0
    if args.news_cmd == "latest":
        path = store.root / "snapshots.jsonl"
        if not path.exists():
            print("no snapshots stored")
        else:
            now = WallClock().now_utc()
            for line in reversed(path.read_text(encoding="utf-8").splitlines()):
                try:
                    snapshot = SentimentSnapshot.model_validate_json(line)
                except ValueError:
                    continue
                if snapshot.expires_at > now:
                    print(snapshot.model_dump_json())
                    break
            else:
                print("no valid, unexpired snapshot")
        return 0
    if args.news_cmd == "schedule":
        print(f"timezone: {config.schedule.timezone}")
        for name in (
            "premarket",
            "midday",
            "pre_cas",
            "postmarket",
            "weekly_proposal",
            "monday_invalidation",
        ):
            print(f"{name}: {getattr(config.schedule, name)}")
        print(
            "Schedule execution is external; invoke `trading news snapshot` "
            "with cron/systemd."
        )
        return 0
    if args.news_cmd == "proposal":
        proposal = abstaining_proposal(as_of=WallClock().now_utc())
        store.append_proposal(proposal)
        print(f"proposal: {proposal.proposal_id} regime=ABSTAIN")
        print(f"valid_until: {proposal.valid_until.isoformat()}")
        return 0
    now = WallClock().now_utc()
    if args.news_cmd == "demo":
        source = next(
            item for item in config.sources if item.tier is NewsSourceTier.OFFICIAL
        )
        item = normalize_news_item(
            source=source,
            url="https://rbi.org.in/demo/local-fixture",
            headline="RBI announces monetary policy decision",
            snippet="Sanitized local demo fixture; no external request made.",
            language="en",
            published_at=now,
            retrieved_at=now,
            provider_metadata={"fixture": True},
            config=config,
        )
        events = cluster_news((item,), as_of=now, config=config)
        classifier = _news_classifier(config)
        snapshot, risks = build_snapshot(
            items=(item,),
            events=events,
            as_of=now,
            config=config,
            classifier=classifier,
        )
        print(f"offline fixture: {item.headline}")
        print(
            f"event clusters: {len(events)}; "
            f"asset impacts: {len(snapshot.asset_impacts)}"
        )
        print(f"event-risk states: {len(risks)}; model: {snapshot.model_version}")
        if isinstance(classifier, FinBertClassifier) and classifier.availability_reason:
            print(f"sentiment unavailable: {classifier.availability_reason}")
        return 0
    start_at = end_at = None
    if args.news_cmd == "backfill":
        start_at = datetime.combine(
            date.fromisoformat(args.start), time.min, tzinfo=UTC
        )
        end_at = min(
            datetime.combine(
                date.fromisoformat(args.end), time(23, 59, 59), tzinfo=UTC
            ),
            now,
        )
    batch = NewsCollector(config).collect(as_of=now, start_at=start_at, end_at=end_at)
    store.append_cycle(batch)
    saved_items = store.append_items(batch.items)
    events = cluster_news(batch.items, as_of=now, config=config)
    store.append_events(events)
    classifier = _news_classifier(config)
    snapshot, risks = build_snapshot(
        items=batch.items,
        events=events,
        as_of=now,
        config=config,
        classifier=classifier,
    )
    store.append_snapshot(snapshot)
    store.append_risks(risks)
    if isinstance(classifier, FinBertClassifier) and classifier.availability_reason:
        print(f"sentiment unavailable: {classifier.availability_reason}")
    if args.news_cmd == "backfill":
        print(f"backfill UTC window: {args.start} through {args.end}")
        print("RSS adapters are limited to items still present in each feed.")
    print(f"cycle={batch.cycle_id} fetched={len(batch.items)} saved={saved_items}")
    print(f"events={len(events)} snapshot={snapshot.snapshot_id}")
    for item in sorted(
        batch.items, key=lambda news_item: news_item.published_at, reverse=True
    )[:5]:
        print(f"news: {item.headline}")
        print(
            f"  source: {item.source_id} | published: {item.published_at.isoformat()}"
        )
        print(f"  url: {item.canonical_url}")
    if not batch.items:
        print("news: no items pulled in this cycle")
    for status in batch.source_health:
        print(f"{status.source_id}: {status.status} fetched={status.fetched_count}")
        if status.reason:
            print(f"  reason: {status.reason}")
    return 0


def _resolve_repo_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else _repo_root() / path


def _evaluation_inputs(
    args: argparse.Namespace,
) -> tuple[CohortPackage, LoadedEvaluationConfig, datetime]:
    """Load a frozen cohort and evaluation policy. Never writes configuration."""
    package = CohortPackage.model_validate_json(
        _resolve_repo_path(args.cohort).read_text(encoding="utf-8")
    )
    loaded = load_evaluation_config(_resolve_repo_path(args.config))
    if args.as_of:
        as_of = ensure_utc(datetime.fromisoformat(args.as_of))
    else:
        as_of = package.observation_end
    return package, loaded, as_of



def _parse_utc(value: str) -> datetime:
    """Parse an ISO-8601 timestamp and require UTC."""
    return ensure_utc(datetime.fromisoformat(value))


def _cmd_evaluate_scorecard(args: argparse.Namespace) -> int:
    try:
        package, loaded, as_of = _evaluation_inputs(args)
        scorecard = build_scorecard(package, loaded.config.fill_model, as_of=as_of)
    except (
        OSError,
        EvaluationError,
        EvaluationConfigError,
        ValidationError,
        ValueError,
    ) as exc:
        print(f"evaluate: {exc}", file=sys.stderr)
        return 1
    print(scorecard.model_dump_json(indent=2))
    return 0


def _cmd_evaluate_eligibility(args: argparse.Namespace) -> int:
    try:
        package, loaded, as_of = _evaluation_inputs(args)
        scorecard = build_scorecard(package, loaded.config.fill_model, as_of=as_of)
        result = evaluate_eligibility(
            scorecard,
            loaded.config.eligibility,
            evaluated_at=as_of,
            threshold_checksum=loaded.checksum,
        )
    except (
        OSError,
        EvaluationError,
        EvaluationConfigError,
        ValidationError,
        ValueError,
    ) as exc:
        print(f"evaluate: {exc}", file=sys.stderr)
        return 1
    print(result.model_dump_json(indent=2))
    return 0


def _cmd_evaluate_judgment(args: argparse.Namespace) -> int:
    try:
        package, loaded, as_of = _evaluation_inputs(args)
        report = evaluate_judgment(
            package,
            loaded.config.fill_model,
            loaded.config.judgment,
            as_of=as_of,
        )
    except (
        OSError,
        JudgmentError,
        EvaluationError,
        EvaluationConfigError,
        ValidationError,
        ValueError,
    ) as exc:
        print(f"evaluate: {exc}", file=sys.stderr)
        return 1
    print(report.model_dump_json(indent=2))
    return 0


def _cmd_evaluate_improvements(args: argparse.Namespace) -> int:
    """Print ranked improvement clusters as JSON (ADESK-A7)."""
    from pathlib import Path as _Path

    from trading.storage.trading_store import TradingStore

    store_path = _Path(args.store)
    store = TradingStore.open(store_path)
    try:
        records = list(store.list_improvement_records())
        if args.as_of:
            as_of = _parse_utc(args.as_of)
            records = list(apply_stale_status(records, as_of=as_of))
        clusters = cluster_improvements(records)
        payload = {
            "cluster_count": len(clusters),
            "record_count": len(records),
            "clusters": [c.model_dump(mode="json") for c in clusters],
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
    finally:
        store.close()
    return 0


def _cmd_evaluate_reviews(args: argparse.Namespace) -> int:
    """Print review-level precision/capture JSON (ADESK-B4)."""
    from trading.analytics.judgment import evaluate_reviews, label_review
    from trading.domain.contracts.review_judgment import ReviewJudgmentRow
    from trading.domain.enums import ReviewAction

    raw = json.loads(Path(args.reviews).read_text(encoding="utf-8"))
    rows: list[ReviewJudgmentRow] = []
    for item in raw:
        subsequent = item.get("subsequent_r")
        action = ReviewAction(item["action"])
        should = item.get("should_hold")
        if should is None and subsequent is not None:
            should = label_review(
                action=action,
                subsequent_r=Decimal(str(subsequent)),
            )
        rows.append(
            ReviewJudgmentRow(
                review_id=item["review_id"],
                trade_id=item["trade_id"],
                action=action,
                should_hold=should,
                held=bool(item["held"]),
                subsequent_r=(None if subsequent is None else Decimal(str(subsequent))),
            )
        )
    as_of = _parse_utc(args.as_of) if args.as_of else datetime.now(tz=UTC)
    report = evaluate_reviews(tuple(rows), as_of=as_of)
    print(report.model_dump_json(indent=2))
    return 0


def _cmd_paper_session(args: argparse.Namespace) -> int:
    """Telegram auth then unattended PAPER loop. Live Fyers is data+OAuth only."""
    return run_paper_session(
        _repo_root(),
        account_config_path=_resolve_repo_path(args.config),
        skip_auth=bool(args.skip_auth),
        once=bool(args.once),
    )


def _cmd_paper_watchdog(_args: argparse.Namespace) -> int:
    """Check protection heartbeat freshness for open PAPER positions."""
    repo = _repo_root()
    session_cfg = load_paper_session_config(repo / "config" / "paper_session.yaml")
    return run_paper_watchdog(
        repo,
        store_path=repo / session_cfg.store_path,
    )


def _cmd_paper_isolate_check(args: argparse.Namespace) -> int:
    """Refuse to proceed unless the process config is PAPER."""
    try:
        loaded = load_config(_resolve_repo_path(args.config))
    except (OSError, ValidationError, ValueError) as exc:
        print(f"paper: {exc}", file=sys.stderr)
        return 1
    if loaded.config.environment is not Environment.PAPER:
        print(
            f"paper: environment is {loaded.config.environment}; "
            "isolate-check requires PAPER",
            file=sys.stderr,
        )
        return 1
    print("paper: Environment.PAPER; live transaction adapters are not constructed")
    return 0


def _cmd_attention_scan(args: argparse.Namespace) -> int:
    try:
        evaluation = load_evaluation_config(_resolve_repo_path(args.config))
        agent = load_agent_config(_resolve_repo_path(args.agent_config))
        base = load_config(_resolve_repo_path(args.base_config))
    except (OSError, ValidationError, ValueError, AgentConfigError) as exc:
        print(f"attention: {exc}", file=sys.stderr)
        return 1
    clock = WallClock()
    ids = SequentialIdFactory(clock.now_utc())
    notify = _attention_sink(bool(args.notify))
    requests = scan_attention_blockers(
        clock=clock,
        id_factory=ids,
        evaluation=evaluation,
        cas_features_complete=False,
        live_unverified_paths=base.config.unverified_paths(),
        agent_enabled=agent.config.enabled,
        notify=notify,
    )
    print(f"attention_requests: {len(requests)}")
    for request in requests:
        print(request.model_dump_json())
    return 0


def _attention_sink(notify: bool) -> AttentionSink | None:
    if not notify:
        return None
    try:
        settings = FyersSettings.from_repo_root(_repo_root())
    except ValidationError:
        print(
            "attention: Telegram credentials missing; CLI output only",
            file=sys.stderr,
        )
        return None
    if not telegram_configured(
        settings.a2a_telegram_bot_token, settings.a2a_telegram_chat_id
    ):
        print(
            "attention: Telegram is not configured; CLI output only",
            file=sys.stderr,
        )
        return None
    return telegram_attention_sink(
        lambda text: send_telegram_message(
            text,
            token=settings.a2a_telegram_bot_token,
            chat_id=settings.a2a_telegram_chat_id,
        )
    )


class _UnavailableLlm:
    def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> LlmTurn:
        raise LlmTimeoutError("no production LLM client is wired")


_FIXTURE_REFUSE_MSG = (
    "refusing fixture/default cohort; pass a data/paper/cohorts/... JSON "
    "or --allow-fixture"
)
_DEFAULT_FIXTURE_COHORT = "tests/fixtures/l4_cohort/long_option.json"


def _is_fixture_cohort_path(root: Path, cohort_path: str) -> bool:
    """True when the resolved path sits under tests/fixtures/."""
    resolved = _resolve_repo_path(cohort_path).resolve()
    fixtures_root = (root / "tests" / "fixtures").resolve()
    return resolved == fixtures_root or fixtures_root in resolved.parents


def _resolve_weekly_cohort(
    root: Path, *, cohort: str, allow_fixture: bool
) -> tuple[str, str]:
    """Return (cohort_path, cohort_source). Raises ValueError to refuse."""
    cohort_path = cohort.strip()
    if not cohort_path:
        if not allow_fixture:
            raise ValueError(_FIXTURE_REFUSE_MSG)
        return _DEFAULT_FIXTURE_COHORT, "fixture"
    if _is_fixture_cohort_path(root, cohort_path):
        if not allow_fixture:
            raise ValueError(_FIXTURE_REFUSE_MSG)
        return cohort_path, "fixture"
    return cohort_path, "paper"


def _agent_budget_store(root: Path, clock: WallClock) -> TradingStore:
    """Open the durable monthly agent budget ledger."""
    path = root / "data" / "agent" / "budget.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    return TradingStore.open(path, clock=clock)


def _cmd_agent_weekly(args: argparse.Namespace) -> int:
    root = _repo_root()
    try:
        evaluation = load_evaluation_config(_resolve_repo_path(args.config))
        agent_loaded = load_agent_config(_resolve_repo_path(args.agent_config))
        cohort_path, cohort_source = _resolve_weekly_cohort(
            root,
            cohort=args.cohort or "",
            allow_fixture=bool(args.allow_fixture),
        )
        package = CohortPackage.model_validate_json(
            _resolve_repo_path(cohort_path).read_text(encoding="utf-8")
        )
    except (OSError, ValidationError, ValueError, AgentConfigError) as exc:
        print(f"agent: {exc}", file=sys.stderr)
        return 1
    clock = WallClock()
    config = agent_loaded.config
    llm: _UnavailableLlm | OpenAICompatLlm | RecordingLlm = _UnavailableLlm()
    recorder: RecordingLlm | None = None
    history: dict[str, Any] = {}
    market = None
    news = None
    model_name = config.model
    llm_settings: LlmSettings | None = None
    budget_store: TradingStore | None = None
    if args.enable:
        config = config.model_copy(update={"enabled": True})
        try:
            llm_settings = load_llm_settings(root)
            inner = OpenAICompatLlm(llm_settings)
        except ValueError as exc:
            print(f"agent: {exc}", file=sys.stderr)
            return 1
        model_name = inner.model
        config = config.model_copy(
            update={
                "enabled": True,
                "model": model_name,
                "max_tokens_per_run": max(config.max_tokens_per_run, 100000),
                "input_inr_per_million_tokens": Decimal("15"),
                "output_inr_per_million_tokens": Decimal("45"),
            }
        )
        recorder = RecordingLlm(inner)
        llm = recorder
        budget_store = _agent_budget_store(root, clock)
        try:
            history, market = _trial_history_port(
                root,
                clock=clock,
                symbol=args.symbol,
                days=args.history_days,
                resolution=args.resolution,
            )
        except (OSError, ValueError, FyersApiError) as exc:
            budget_store.close()
            print(f"agent history: {exc}", file=sys.stderr)
            return 1
        news = _trial_news_port(root)
    ctx = ToolContext(
        clock=clock,
        id_factory=SequentialIdFactory(clock.now_utc()),
        evaluation=evaluation,
        cohort=package,
        market=market,
        news=news,
        model_name=model_name,
        agent_role="weekly",
        budget_store=budget_store,
    )
    prompt = (
        "Propose STRATEGY_FAMILY stances for the next week using tools. "
        "Markets may be closed; fetch_market is Fyers historical bars. "
        f"Start with fetch_market for {args.symbol}, then scorecard and "
        "eligibility. This run cannot place orders or change live config."
    )
    proposal = run_weekly_agent(
        llm=llm,
        config=config,
        tools=ctx,
        prompt=prompt,
    )
    print(proposal.model_dump_json(indent=2))
    if recorder is not None:
        stamp = clock.now_utc().strftime("%Y%m%dT%H%M%SZ")
        out_dir = Path(args.out_dir)
        if not out_dir.is_absolute():
            out_dir = root / out_dir
        run_dir = out_dir / f"{stamp}-{proposal.proposal_id}"
        persist_agent_run(
            out_dir=run_dir,
            proposal=proposal,
            turns=recorder.turns,
            history=history,
            meta={
                "model": model_name,
                "resolved_model_id": recorder.resolved_model_id or model_name,
                "llm_temperature": (
                    llm_settings.llm_temperature if llm_settings is not None else None
                ),
                "llm_seed": llm_settings.llm_seed if llm_settings is not None else None,
                "enabled_override": True,
                "shipped_agent_enabled": agent_loaded.config.enabled,
                "symbol": args.symbol,
                "history_days": args.history_days,
                "resolution": args.resolution,
                "cohort": cohort_path,
                "cohort_source": cohort_source,
            },
            attention=tuple(ctx.attention_requests),
        )
        print(f"reasoning: {run_dir / 'reasoning.md'}")
        print(f"proposal: {run_dir / 'proposal.json'}")
        print(f"transcript: {run_dir / 'transcript.jsonl'}")
    if budget_store is not None:
        budget_store.close()
    return 0


def _cmd_agent_advise(args: argparse.Namespace) -> int:
    """Rank paper structures into StructureAdvice. Never ENABLE/live."""
    root = _repo_root()
    try:
        evaluation = load_evaluation_config(_resolve_repo_path(args.config))
        agent_loaded = load_agent_config(_resolve_repo_path(args.agent_config))
        cohort_path, cohort_source = _resolve_weekly_cohort(
            root,
            cohort=args.cohort or "",
            allow_fixture=bool(args.allow_fixture),
        )
        package = CohortPackage.model_validate_json(
            _resolve_repo_path(cohort_path).read_text(encoding="utf-8")
        )
    except (OSError, ValidationError, ValueError, AgentConfigError) as exc:
        print(f"agent: {exc}", file=sys.stderr)
        return 1
    clock = WallClock()
    config = agent_loaded.config
    llm: _UnavailableLlm | OpenAICompatLlm | RecordingLlm = _UnavailableLlm()
    recorder: RecordingLlm | None = None
    history: dict[str, Any] = {}
    market = None
    news = None
    model_name = config.model
    llm_settings: LlmSettings | None = None
    budget_store: TradingStore | None = None
    if args.enable:
        config = config.model_copy(update={"enabled": True})
        try:
            llm_settings = load_llm_settings(root)
            inner = OpenAICompatLlm(llm_settings)
        except ValueError as exc:
            print(f"agent: {exc}", file=sys.stderr)
            return 1
        model_name = inner.model
        config = config.model_copy(
            update={
                "enabled": True,
                "model": model_name,
                "max_tokens_per_run": max(config.max_tokens_per_run, 100000),
                "input_inr_per_million_tokens": Decimal("15"),
                "output_inr_per_million_tokens": Decimal("45"),
            }
        )
        recorder = RecordingLlm(inner)
        llm = recorder
        budget_store = _agent_budget_store(root, clock)
        try:
            history, market = _trial_history_port(
                root,
                clock=clock,
                symbol=args.symbol,
                days=args.history_days,
                resolution=args.resolution,
            )
        except (OSError, ValueError, FyersApiError) as exc:
            budget_store.close()
            print(f"agent history: {exc}", file=sys.stderr)
            return 1
        news = _trial_news_port(root)
    ctx = ToolContext(
        clock=clock,
        id_factory=SequentialIdFactory(clock.now_utc()),
        evaluation=evaluation,
        cohort=package,
        market=market,
        news=news,
        model_name=model_name,
        agent_role="advise",
        budget_store=budget_store,
    )
    prompt = (
        "Rank paper structures for the next session using tools. "
        "Prefer PASS when evidence is thin. Never ENABLE or speak as live. "
        f"Start with fetch_market for {args.symbol}, then scorecard and "
        "eligibility. This run cannot place orders or change live config."
    )
    advice = run_advise_agent(
        llm=llm,
        config=config,
        tools=ctx,
        prompt=prompt,
    )
    print(advice.model_dump_json(indent=2))
    stamp = clock.now_utc().strftime("%Y%m%dT%H%M%SZ")
    out_dir = Path(args.out_dir)
    if not out_dir.is_absolute():
        out_dir = root / out_dir
    run_dir = out_dir / f"{stamp}-advise-{advice.preferred_structure.value}"
    run_dir.mkdir(parents=True, exist_ok=True)
    advice_path = run_dir / "advice.json"
    advice_path.write_text(advice.model_dump_json(indent=2) + "\n", encoding="utf-8")
    meta = {
        "model": model_name,
        "resolved_model_id": (
            recorder.resolved_model_id if recorder is not None else model_name
        ),
        "llm_temperature": (
            llm_settings.llm_temperature if llm_settings is not None else None
        ),
        "llm_seed": llm_settings.llm_seed if llm_settings is not None else None,
        "enabled_override": bool(args.enable),
        "shipped_agent_enabled": agent_loaded.config.enabled,
        "symbol": args.symbol,
        "history_days": args.history_days,
        "resolution": args.resolution,
        "cohort": cohort_path,
        "cohort_source": cohort_source,
        "preferred_structure": advice.preferred_structure.value,
        "stance": advice.stance.value,
    }
    (run_dir / "meta.json").write_text(
        json.dumps(meta, indent=2) + "\n", encoding="utf-8"
    )
    if recorder is not None:
        (run_dir / "transcript.jsonl").write_text(
            "".join(json.dumps(turn, default=str) + "\n" for turn in recorder.turns),
            encoding="utf-8",
        )
        (run_dir / "history.json").write_text(
            json.dumps(history, indent=2, default=str) + "\n", encoding="utf-8"
        )
        requests = [
            {"turn": index, "request_body": turn.get("request_body")}
            for index, turn in enumerate(recorder.turns, start=1)
            if isinstance(turn.get("request_body"), dict)
        ]
        if requests:
            (run_dir / "requests.json").write_text(
                json.dumps(requests, indent=2, default=str) + "\n",
                encoding="utf-8",
            )
        responses = [
            {"turn": index, "raw_response": turn.get("raw_response")}
            for index, turn in enumerate(recorder.turns, start=1)
            if isinstance(turn.get("raw_response"), dict)
        ]
        if responses:
            (run_dir / "raw_responses.json").write_text(
                json.dumps(responses, indent=2, default=str) + "\n",
                encoding="utf-8",
            )
    if budget_store is not None:
        budget_store.close()
    print(f"advice: {advice_path}")
    return 0


def _trial_history_port(
    root: Path,
    *,
    clock: WallClock,
    symbol: str,
    days: int,
    resolution: str,
) -> tuple[dict[str, Any], SnapshotMarketPort]:
    pipeline = load_data_pipeline_config(root / "config" / "data_pipeline.yaml")
    targets = [item for item in pipeline.underlyings if item.symbol == symbol]
    if not targets:
        raise ValueError(f"no underlying matching {symbol!r}")
    store = JsonlEventStore(root / pipeline.storage.root)
    try:
        feed = _fyers_feed(root)
        results = backfill_history(
            pipeline_config=pipeline,
            repo_root=root,
            feed=feed,
            store=store,
            clock=clock,
            underlying=targets[0],
            resolutions=(resolution,),
            days=days,
            catalog=None,
        )
        end = clock.now_utc().astimezone(UTC).date()
        start = end - timedelta(days=max(days, 1))
        capture = feed.fetch_history(
            symbol,
            resolution=resolution,
            range_from=start.isoformat(),
            range_to=end.isoformat(),
        )
        event = normalize_fyers_history(
            capture,
            symbol=symbol,
            resolution=resolution,
            normalization_version=pipeline.normalization_version,
            raw_ref="agent-trial",
        )
        bars = _payload_bars(event.payload)[-80:]
        snapshot = history_evidence(
            symbol=symbol,
            resolution=resolution,
            bars=bars,
            published_at=event.source_time,
            clock=clock,
        )
        history: dict[str, Any] = {
            "source": "fyers-live-history",
            "backfill_events": [item.event_ids for item in results],
            "snapshot": snapshot,
        }
        return history, SnapshotMarketPort({symbol: snapshot})
    except FyersApiError as exc:
        print(
            f"agent history: live Fyers failed ({exc}); using local store",
            file=sys.stderr,
        )
        return _local_history_port(store, symbol=symbol, clock=clock)


def _local_history_port(
    store: JsonlEventStore,
    *,
    symbol: str,
    clock: WallClock,
) -> tuple[dict[str, Any], SnapshotMarketPort]:
    events = store.read_canonical(
        symbol=symbol,
        start=datetime(2020, 1, 1, tzinfo=UTC),
        end=clock.now_utc(),
    )
    bars_events = [item for item in events if item.event_type == "BAR_SNAPSHOT"]
    if not bars_events:
        raise ValueError(
            "no local BAR_SNAPSHOT for this symbol; re-auth with "
            "'trading auth fyers' and retry"
        )
    latest = bars_events[-1]
    bars = _payload_bars(latest.payload)[-80:]
    resolution = str(latest.payload.get("resolution") or "unknown")
    snapshot = history_evidence(
        symbol=symbol,
        resolution=resolution,
        bars=bars,
        published_at=latest.source_time,
        clock=clock,
    )
    snapshot["source"] = "fyers-history-local"
    snapshot["note"] = (
        "Fyers live history unavailable; last persisted Fyers BAR_SNAPSHOT used"
    )
    history = {
        "source": "local-canonical",
        "event_id": latest.event_id,
        "snapshot": snapshot,
    }
    return history, SnapshotMarketPort({symbol: snapshot})


def _payload_bars(payload: dict[str, Any]) -> list[dict[str, Any]]:
    bars_raw = payload.get("bars", [])
    if not isinstance(bars_raw, list):
        return []
    return [row for row in bars_raw if isinstance(row, dict)]


def _trial_news_port(root: Path) -> StaticNewsPort:
    path = root / "data" / "macro_news.jsonl"
    if not path.is_file():
        return StaticNewsPort({"items": [], "note": "no data/macro_news.jsonl on disk"})
    loaded = load_macro_news_jsonl(path)
    return StaticNewsPort(
        {
            "item_count": len(loaded.items),
            "errors": list(loaded.errors),
            "items": [item.model_dump(mode="json") for item in loaded.items[:20]],
        }
    )


def _cmd_dashboard(args: argparse.Namespace) -> int:
    """Serve the read-only terminal or emit one machine-readable snapshot."""
    from trading.dashboard import (
        DashboardConfig,
        build_dashboard_snapshot,
        serve_dashboard,
    )

    root = _repo_root()
    if args.dashboard_cmd == "snapshot":
        print(json.dumps(build_dashboard_snapshot(root, source=args.source)))
        return 0
    oracle_host = args.oracle_host or os.environ.get(
        "TRADING_DASHBOARD_ORACLE_HOST", ""
    )
    key_raw = args.ssh_key or os.environ.get("TRADING_DASHBOARD_SSH_KEY", "")
    oracle_root = args.oracle_root or os.environ.get(
        "TRADING_DASHBOARD_ORACLE_ROOT", "~/fno-automated"
    )
    serve_dashboard(
        DashboardConfig(
            repo_root=root,
            bind_host=args.bind,
            port=args.port,
            oracle_host=oracle_host,
            ssh_key=Path(key_raw).expanduser() if key_raw else None,
            oracle_root=oracle_root,
            refresh_seconds=args.refresh_seconds,
            open_browser=not args.no_browser,
        )
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="trading")
    sub = parser.add_subparsers(dest="command", required=True)

    auth = sub.add_parser("auth", help="broker authentication helpers")
    auth_sub = auth.add_subparsers(dest="broker", required=True)
    fyers_auth = auth_sub.add_parser("fyers", help="interactive Fyers OAuth login")
    fyers_auth.add_argument(
        "--auth-code",
        default="",
        help="skip browser prompt if you already have an auth_code",
    )
    fyers_auth.add_argument(
        "--no-browser",
        action="store_true",
        help="print login URL only; do not open a browser",
    )
    fyers_auth.set_defaults(func=_cmd_auth_fyers)
    fyers_refresh = auth_sub.add_parser(
        "refresh", help="refresh access token from the stored refresh token"
    )
    fyers_refresh.set_defaults(func=_cmd_auth_refresh)
    fyers_telegram = auth_sub.add_parser(
        "telegram",
        help="send the login URL to Telegram and exchange the pasted redirect URL",
    )
    fyers_telegram.set_defaults(func=_cmd_auth_telegram)

    data = sub.add_parser("data", help="market data pipeline")
    data_sub = data.add_subparsers(dest="data_cmd", required=True)
    fetch = data_sub.add_parser(
        "fetch", help="pull quotes, bars, option chain and reference once"
    )
    fetch.add_argument("--symbol", default="", help="Fyers symbol filter")
    fetch.set_defaults(func=_cmd_data_fetch)
    stream = data_sub.add_parser("stream", help="subscribe to websocket ticks")
    stream.add_argument("--symbol", default="", help="Fyers symbol filter")
    stream.add_argument(
        "--max-ticks",
        type=int,
        default=0,
        help="stop after N ticks (default: config fyers.ws_max_ticks)",
    )
    stream.add_argument(
        "--duration",
        type=int,
        default=0,
        help="max seconds to run (default: config fyers.ws_duration_seconds)",
    )
    stream.add_argument(
        "--daemon",
        action="store_true",
        help="run until SIGTERM; persist ticks and reconnect on failure",
    )
    stream.set_defaults(func=_cmd_data_stream)
    replay = data_sub.add_parser("replay", help="replay stored canonical events")
    replay.add_argument("--symbol", default="", help="Fyers symbol filter")
    replay.add_argument("--hours", type=int, default=24, help="lookback window")
    replay.set_defaults(func=_cmd_data_replay)
    backfill = data_sub.add_parser(
        "backfill", help="download instrument masters or historical bars"
    )
    backfill_sub = backfill.add_subparsers(dest="backfill_cmd", required=True)
    instruments = backfill_sub.add_parser(
        "instruments", help="refresh Fyers symbol-master catalog"
    )
    instruments.set_defaults(func=_cmd_data_backfill_instruments)
    history = backfill_sub.add_parser(
        "history", help="pull chunked OHLCV history into canonical storage"
    )
    history.add_argument("--symbol", default="", help="Fyers symbol filter")
    history.add_argument(
        "--resolutions",
        default="",
        help="comma-separated Fyers resolutions (default: config bar_resolutions)",
    )
    history.add_argument(
        "--days",
        type=int,
        default=0,
        help="inclusive lookback in UTC days (default: config fyers.bar_lookback_days)",
    )
    history.set_defaults(func=_cmd_data_backfill_history)
    news = data_sub.add_parser("news", help="macro news JSONL helpers")
    news_sub = news.add_subparsers(dest="news_cmd", required=True)
    validate = news_sub.add_parser("validate", help="check macro_news.jsonl records")
    validate.add_argument(
        "--file",
        default="",
        help="JSONL path (default: macro_news.input_file from config)",
    )
    validate.set_defaults(func=_cmd_data_news_validate)

    news_ops = sub.add_parser("news", help="advisory news and macro evidence pipeline")
    news_ops_sub = news_ops.add_subparsers(dest="news_cmd", required=True)
    for command, help_text in (
        ("collect", "collect one configured cycle"),
        ("backfill", "bounded provider-window collection"),
        ("snapshot", "collect and persist a snapshot"),
        ("demo", "show offline subsystem configuration"),
        ("status", "show local store counts"),
        ("latest", "show the latest persisted snapshot"),
        ("schedule", "show configured local-time windows"),
        ("proposal", "write an abstaining weekly proposal"),
    ):
        operation = news_ops_sub.add_parser(command, help=help_text)
        operation.add_argument("--config", default="config/news.yaml")
        operation.add_argument("--store", default="data/news")
        if command == "backfill":
            operation.add_argument("--start", required=True, help="UTC start date")
            operation.add_argument("--end", required=True, help="UTC end date")
        operation.set_defaults(func=_cmd_news)

    evaluate = sub.add_parser(
        "evaluate",
        help="read-only Layer 4 scorecard and eligibility reports",
    )
    evaluate_sub = evaluate.add_subparsers(dest="evaluate_cmd", required=True)
    for command, help_text, handler in (
        (
            "scorecard",
            "print a deterministic cohort scorecard as JSON",
            _cmd_evaluate_scorecard,
        ),
        (
            "eligibility",
            "print a fail-closed promotion eligibility report as JSON",
            _cmd_evaluate_eligibility,
        ),
        (
            "judgment",
            "print offline should_enter/should_pass judgment metrics as JSON",
            _cmd_evaluate_judgment,
        ),
    ):
        operation = evaluate_sub.add_parser(command, help=help_text)
        operation.add_argument("cohort", help="path to a frozen CohortPackage JSON")
        operation.add_argument(
            "--config",
            default="config/evaluation.yaml",
            help="evaluation policy path (read-only)",
        )
        operation.add_argument(
            "--as-of",
            default="",
            help="UTC evaluation instant (default: cohort observation_end)",
        )

    # ADESK-A7: improvements uses a store path, not a cohort package.
    improvements_parser = evaluate_sub.add_parser(
        "improvements",
        help="print ranked ImprovementRecord clusters as JSON",
    )
    improvements_parser.add_argument(
        "--store",
        default="data/trading.sqlite",
        help="path to TradingStore sqlite file",
    )
    improvements_parser.add_argument(
        "--as-of",
        default="",
        help="UTC instant for STALE marking (optional)",
    )
    improvements_parser.set_defaults(func=_cmd_evaluate_improvements)

    reviews_parser = evaluate_sub.add_parser(
        "reviews",
        help="print review-level precision and capture as JSON",
    )
    reviews_parser.add_argument(
        "reviews",
        help="path to JSON list of review rows",
    )
    reviews_parser.add_argument(
        "--as-of",
        default="",
        help="UTC evaluation instant (default: now)",
    )
    reviews_parser.set_defaults(func=_cmd_evaluate_reviews)

    paper = sub.add_parser("paper", help="supervised PAPER runner helpers")
    paper_sub = paper.add_subparsers(dest="paper_cmd", required=True)
    isolate = paper_sub.add_parser(
        "isolate-check",
        help="verify PAPER environment cannot select a live transaction adapter",
    )
    isolate.add_argument("--config", default="config/base.yaml")
    isolate.set_defaults(func=_cmd_paper_isolate_check)
    session = paper_sub.add_parser(
        "session",
        help="unattended PAPER session after Telegram Fyers login",
    )
    session.add_argument("--config", default="config/paper.yaml")
    session.add_argument(
        "--skip-auth",
        action="store_true",
        help="skip Telegram OAuth (tests and already-cached tokens)",
    )
    session.add_argument(
        "--once",
        action="store_true",
        help="run one cycle then exit (no sleep loop)",
    )
    session.set_defaults(func=_cmd_paper_session)
    watchdog = paper_sub.add_parser(
        "watchdog",
        help="verify protection heartbeat freshness for open PAPER positions",
    )
    watchdog.set_defaults(func=_cmd_paper_watchdog)

    attention = sub.add_parser("attention", help="operator attention requests")
    attention_sub = attention.add_subparsers(dest="attention_cmd", required=True)
    scan = attention_sub.add_parser("scan", help="print current Layer 4 blockers")
    scan.add_argument("--config", default="config/evaluation.yaml")
    scan.add_argument("--agent-config", default="config/agent.yaml")
    scan.add_argument("--base-config", default="config/base.yaml")
    scan.add_argument(
        "--notify",
        action="store_true",
        help="also send blockers to Telegram when credentials are configured",
    )
    scan.set_defaults(func=_cmd_attention_scan)

    agent = sub.add_parser("agent", help="Layer 4 weekly proposal loop")
    agent_sub = agent.add_subparsers(dest="agent_cmd", required=True)
    weekly = agent_sub.add_parser(
        "weekly", help="run the bounded weekly agent (ABSTAIN when disabled)"
    )
    weekly.add_argument("cohort", nargs="?", default="", help="CohortPackage JSON")
    weekly.add_argument("--config", default="config/evaluation.yaml")
    weekly.add_argument("--agent-config", default="config/agent.yaml")
    weekly.add_argument(
        "--enable",
        action="store_true",
        help="in-memory enable plus DeepSeek; shipped agent.yaml stays disabled",
    )
    weekly.add_argument(
        "--allow-fixture",
        action="store_true",
        help="explicitly allow tests/fixtures/** cohorts (demo only)",
    )
    weekly.add_argument("--symbol", default="NSE:NIFTY50-INDEX")
    weekly.add_argument("--history-days", type=int, default=20)
    weekly.add_argument("--resolution", default="D")
    weekly.add_argument("--out-dir", default="data/paper/agent_runs")
    weekly.set_defaults(func=_cmd_agent_weekly)

    advise = agent_sub.add_parser(
        "advise",
        help="rank paper structures (PASS when disabled; never ENABLE/live)",
    )
    advise.add_argument("cohort", nargs="?", default="", help="CohortPackage JSON")
    advise.add_argument("--config", default="config/evaluation.yaml")
    advise.add_argument("--agent-config", default="config/agent.yaml")
    advise.add_argument(
        "--enable",
        action="store_true",
        help="in-memory enable plus DeepSeek; shipped agent.yaml stays disabled",
    )
    advise.add_argument(
        "--allow-fixture",
        action="store_true",
        help="explicitly allow tests/fixtures/** cohorts (demo only)",
    )
    advise.add_argument("--symbol", default="NSE:NIFTY50-INDEX")
    advise.add_argument("--history-days", type=int, default=20)
    advise.add_argument("--resolution", default="D")
    advise.add_argument("--out-dir", default="data/paper/agent_runs")
    advise.set_defaults(func=_cmd_agent_advise)

    dashboard = sub.add_parser("dashboard", help="read-only local Oracle desk terminal")
    dashboard_sub = dashboard.add_subparsers(dest="dashboard_cmd", required=True)
    dashboard_snapshot = dashboard_sub.add_parser(
        "snapshot", help="print one redacted telemetry snapshot as JSON"
    )
    dashboard_snapshot.add_argument(
        "--source", default="local", help="snapshot source label"
    )
    dashboard_snapshot.set_defaults(func=_cmd_dashboard)
    dashboard_serve = dashboard_sub.add_parser(
        "serve", help="serve the local dashboard, optionally probing Oracle over SSH"
    )
    dashboard_serve.add_argument("--bind", default="127.0.0.1")
    dashboard_serve.add_argument("--port", type=int, default=8765)
    dashboard_serve.add_argument(
        "--oracle-host", default="", help="SSH target, for example ubuntu@host"
    )
    dashboard_serve.add_argument(
        "--ssh-key", default="", help="private key used only by the local SSH client"
    )
    dashboard_serve.add_argument(
        "--oracle-root", default="", help="repository path on Oracle"
    )
    dashboard_serve.add_argument("--refresh-seconds", type=int, default=5)
    dashboard_serve.add_argument("--no-browser", action="store_true")
    dashboard_serve.set_defaults(func=_cmd_dashboard)

    args = parser.parse_args(argv)
    func = getattr(args, "func", None)
    if func is None:
        parser.print_help()
        return 1
    return int(func(args))


if __name__ == "__main__":
    raise SystemExit(main())
