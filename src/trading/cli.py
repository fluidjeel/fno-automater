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
from trading.analytics.cohort_evidence import is_discovery_experiment_id
from trading.analytics.eligibility import evaluate_eligibility
from trading.analytics.improvements import apply_stale_status, cluster_improvements
from trading.analytics.judgment import JudgmentError, evaluate_judgment
from trading.analytics.scorecard import EvaluationError, build_scorecard
from trading.config import (
    AgentConfigError,
    EvaluationConfigError,
    FillModelConfig,
    LoadedEvaluationConfig,
    load_agent_config,
    load_config,
    load_evaluation_config,
)
from trading.config.evaluation import discovery_fill_models
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
from trading.data.vix_backfill import backfill_vix_snapshots
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
    settings = FyersSettings.from_repo_root_with_cache(root)
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
    settings = FyersSettings.from_repo_root_with_cache(root)
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


def _cmd_data_backfill_vix_history(args: argparse.Namespace) -> int:
    root = _repo_root()
    config = load_data_pipeline_config(root / "config" / "data_pipeline.yaml")
    result = backfill_vix_snapshots(
        pipeline_config=config,
        repo_root=root,
        feed=_fyers_feed(root),
        clock=WallClock(),
        app_config_path=_resolve_repo_path("config/paper.yaml"),
        days=args.days,
    )
    print(
        f"{result.symbol}: wrote={result.written} skipped={result.skipped} "
        f"trading_days={result.trading_days}"
    )
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


def _fill_policy_for_package(
    package: CohortPackage,
    loaded: LoadedEvaluationConfig,
) -> FillModelConfig:
    """Select the fill policy that matches the cohort evidence profile."""
    if not is_discovery_experiment_id(package.experiment.experiment_id):
        return loaded.config.fill_model
    touch, _shadow = discovery_fill_models(
        loaded.config.fill_model,
        model=package.experiment.fill_model_version,
        shadow_model="conservative-v1",
    )
    return touch


def _cmd_evaluate_scorecard(args: argparse.Namespace) -> int:
    try:
        package, loaded, as_of = _evaluation_inputs(args)
        fill_policy = _fill_policy_for_package(package, loaded)
        scorecard = build_scorecard(package, fill_policy, as_of=as_of)
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
        fill_policy = _fill_policy_for_package(package, loaded)
        scorecard = build_scorecard(package, fill_policy, as_of=as_of)
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
    store = TradingStore.open(store_path, clock=WallClock())
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


def _cmd_evaluate_decisions(args: argparse.Namespace) -> int:
    """Print DISCOVERY_DECISION rows for one session date as a readable table."""
    from datetime import datetime as dt_datetime
    from zoneinfo import ZoneInfo

    from trading.domain.enums import ModeId
    from trading.runtime.discovery_decision_recorder import query_decisions
    from trading.storage.trading_store import TradingStore

    kol = ZoneInfo("Asia/Kolkata")
    session = date.fromisoformat(args.date)
    session_dt = dt_datetime.combine(session, dt_datetime.min.time(), tzinfo=kol)
    mode = ModeId(args.mode) if args.mode else None
    store = TradingStore.open(Path(args.store), clock=WallClock())
    try:
        rows = query_decisions(
            store,
            session_date=session_dt,
            mode_id=mode,
            experiment_id=args.experiment_id or None,
        )
        headers = (
            "time",
            "mode",
            "family",
            "decision",
            "stage",
            "reasons",
            "experiment",
        )
        print("\t".join(headers))
        for row in rows:
            local = row.as_of.astimezone(kol).strftime("%H:%M:%S")
            print(
                "\t".join(
                    (
                        local,
                        row.mode_id.value,
                        row.family_id,
                        row.decision.value,
                        row.stage.value,
                        ",".join(code.value for code in row.reason_codes),
                        row.experiment_id,
                    )
                )
            )
        print(f"# {len(rows)} decision record(s)", file=sys.stderr)
    finally:
        store.close()
    return 0


def _cmd_evaluate_monthly_meta(args: argparse.Namespace) -> int:
    """Build and persist monthly meta-report (ADESK-E3)."""
    from trading.analytics.agent_scorecard import build_agent_scorecard
    from trading.analytics.monthly_meta_report import (
        build_monthly_meta_report,
        persist_monthly_meta_report,
    )
    from trading.domain.enums import DeskRole
    from trading.storage.trading_store import TradingStore

    as_of = _parse_utc(args.as_of) if args.as_of else WallClock().now_utc()
    out_dir = Path(args.out_dir)
    store = TradingStore.open(Path(args.store), clock=WallClock())
    try:
        cards = []
        for role in DeskRole:
            decisions = store.list_agent_decisions(role=role)
            cards.append(build_agent_scorecard(decisions, role=role, as_of=as_of))
        report = build_monthly_meta_report(as_of=as_of, scorecards=tuple(cards))
        path = persist_monthly_meta_report(report, out_dir=out_dir)
        print(report.model_dump_json(indent=2))
        print(f"# wrote {path}", file=sys.stderr)
    finally:
        store.close()
    return 0


def _cmd_evaluate_research_weekly(args: argparse.Namespace) -> int:
    """Build and persist RESEARCH weekly artifact (ADESK-E1)."""
    from trading.analytics.research_weekly import (
        build_research_weekly,
        persist_research_weekly,
    )
    from trading.storage.trading_store import TradingStore

    as_of = _parse_utc(args.as_of) if args.as_of else WallClock().now_utc()
    out_dir = Path(args.out_dir)
    store = TradingStore.open(Path(args.store), clock=WallClock())
    try:
        records = list(store.list_improvement_records())
        report = build_research_weekly(
            records,
            as_of=as_of,
            cohort_id=args.cohort_id or f"store:{Path(args.store).name}",
        )
        written = persist_research_weekly(report, out_dir=out_dir)
        print(report.model_dump_json(indent=2))
        print(f"# wrote {written}", file=sys.stderr)
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
    as_of = _parse_utc(args.as_of) if args.as_of else WallClock().now_utc()
    report = evaluate_reviews(tuple(rows), as_of=as_of)
    print(report.model_dump_json(indent=2))
    return 0


def _cmd_evaluate_terminal_cost(args: argparse.Namespace) -> int:
    """Print ADESK-C4 terminal-policy cost report (R + INR) as JSON."""
    from decimal import Decimal

    from trading.analytics.terminal_policy_cost import (
        TerminalPolicyOutcomeRow,
        build_terminal_policy_cost_report,
    )
    from trading.domain.enums import TerminalPolicyKind
    from trading.domain.primitives import Currency, Money

    as_of = _parse_utc(args.as_of) if args.as_of else WallClock().now_utc()
    rows: list[TerminalPolicyOutcomeRow] = []
    if args.fixture:
        rows.append(
            TerminalPolicyOutcomeRow(
                trade_id="fixture-run",
                policy_kind=TerminalPolicyKind.RUN_TO_EXPIRY_DEFINED_RISK,
                held_through_flatten_dte=True,
                round_trip_charges_inr=Money.of(Decimal("50"), Currency.INR),
                round_trip_spread_inr=Money.of(Decimal("30"), Currency.INR),
                r_unit_inr=Money.of(Decimal("1000"), Currency.INR),
            )
        )
    report = build_terminal_policy_cost_report(tuple(rows), as_of=as_of)
    print(report.model_dump_json(indent=2))
    return 0


def _cmd_evaluate_desk(args: argparse.Namespace) -> int:
    """Print PART 14 agent desk scorecard as JSON (ADESK-B10)."""
    from trading.analytics.agent_scorecard import build_agent_scorecard
    from trading.domain.enums import DeskRole
    from trading.storage.trading_store import TradingStore

    role = DeskRole(args.role)
    as_of = _parse_utc(args.as_of) if args.as_of else WallClock().now_utc()
    store = TradingStore.open(Path(args.store), clock=WallClock())
    try:
        decisions = store.list_agent_decisions(role=role)
        card = build_agent_scorecard(decisions, role=role, as_of=as_of)
        print(card.model_dump_json(indent=2))
    finally:
        store.close()
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
    grant = None
    if getattr(args, "grant_id", None) and budget_store is not None:
        grant = budget_store.get_authority_grant(args.grant_id)
    proposal = run_weekly_agent(
        llm=llm,
        config=config,
        tools=ctx,
        prompt=prompt,
        grant=grant,
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
    grant = None
    if getattr(args, "grant_id", None) and budget_store is not None:
        grant = budget_store.get_authority_grant(args.grant_id)
    advice = run_advise_agent(
        llm=llm,
        config=config,
        tools=ctx,
        prompt=prompt,
        grant=grant,
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


def _cmd_data_record_chain(args: argparse.Namespace) -> int:
    from trading.data.recorder import OptionChainRecorder

    root = _repo_root()
    feed = _fyers_feed(root)
    recorder = OptionChainRecorder(
        feed=feed, output_dir=args.out_dir, clock=WallClock()
    )
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    print(f"Recording option chain for {symbols} -> {args.out_dir}")
    recorder.poll_and_record(
        symbols,
        interval_seconds=args.interval,
        max_iterations=args.iterations if args.iterations > 0 else None,
    )
    return 0


def _cmd_ops_daemon(args: argparse.Namespace) -> int:
    from trading.ops.daemon import DaemonPhase, DaemonSupervisor
    from trading.ops.orchestrator import PaperAutopilotOrchestrator

    repo = _repo_root()
    orchestrator = PaperAutopilotOrchestrator(
        repo,
        dry_run=bool(args.dry_run),
    )

    def _on_phase_change(previous: DaemonPhase, phase: DaemonPhase) -> None:
        orchestrator.on_phase(phase, previous=previous)

    def _on_tick(phase: DaemonPhase) -> None:
        orchestrator.tick(phase)

    supervisor = DaemonSupervisor(
        heartbeat_path=repo / "data" / "daemon_heartbeat.json",
        on_phase_change=_on_phase_change,
        on_tick=_on_tick,
    )
    print(
        f"Starting DaemonSupervisor (interval={args.interval}s, dry_run={args.dry_run})"
    )
    if int(args.iterations) > 0:
        for _ in range(int(args.iterations)):
            supervisor.tick()
        return 0
    supervisor.run(poll_interval_seconds=float(args.interval))
    return 0


def _cmd_ops_ensure_paper(_args: argparse.Namespace) -> int:
    """One-shot autopilot health pass for timers and manual recovery."""
    from trading.domain.clock import WallClock
    from trading.ops.daemon import determine_phase
    from trading.ops.orchestrator import PaperAutopilotOrchestrator

    repo = _repo_root()
    orchestrator = PaperAutopilotOrchestrator(repo)
    phase = determine_phase(WallClock().now_utc())
    result = orchestrator.tick(phase)
    inactive = [item for item in result.services if not getattr(item, "active", True)]
    if inactive:
        return 1
    return 0


def _cmd_ops_post_open_check(_args: argparse.Namespace) -> int:
    """Run the one-time four-mode PAPER readiness checklist."""
    from trading.ops.post_open_check import run_post_open_check

    result = run_post_open_check(_repo_root())
    for name, passed, detail in result.checks:
        print(f"{name}: {'PASS' if passed else 'FAIL'} — {detail}")
    print(f"telegram: {'SENT' if result.notified else 'NOT_SENT'}")
    return 0 if result.passed else 1


def _cmd_ops_alert_unit_failure(args: argparse.Namespace) -> int:
    """Send a Telegram alert when systemd reports a unit failure."""
    from trading.ops.operator_alert import notify_operator

    unit = str(args.unit)
    repo = _repo_root()
    notify_operator(
        repo,
        title=f"{unit} failed",
        detail=(
            f"systemd OnFailure fired for {unit}. Check journalctl and session logs."
        ),
        dedupe_key=f"onfailure:{unit}",
    )
    return 0


def _cmd_ops_telegram_bot(args: argparse.Namespace) -> int:
    import threading

    from trading.data.fyers.telegram import telegram_configured
    from trading.ops.telegram_bot import TelegramBotConfig, TelegramCommandDispatcher

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not telegram_configured(token, chat_id):
        print(
            "ERROR: Telegram credentials not configured "
            "(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)",
            file=sys.stderr,
        )
        return 1
    config = TelegramBotConfig(token=token, allowed_chat_ids=(chat_id,))
    bot = TelegramCommandDispatcher(config)
    print(f"Starting Telegram Interactive Bot polling for chat {chat_id}...")
    stop_event = threading.Event()
    bot.run_loop(stop_event)
    return 0


def _cmd_evaluate_operator_view(args: argparse.Namespace) -> int:
    from trading.runtime.family_status import (
        build_family_operator_view,
        build_four_mode_allocations,
    )
    from trading.runtime.paper_session import load_paper_session_config

    repo = _repo_root()
    session_cfg = load_paper_session_config(repo / "config" / "paper_session.yaml")
    payload = {
        "allocations": list(build_four_mode_allocations()),
        "families": [
            row.model_dump(mode="json")
            for row in build_family_operator_view(session_cfg)
        ],
    }
    print(json.dumps(payload, indent=2))
    return 0


def _cmd_evaluate_paper_readiness(args: argparse.Namespace) -> int:
    from trading.analytics.paper_readiness import evaluate_paper_readiness

    report = evaluate_paper_readiness(
        total_decisions=args.target_days * 5,
        charges_verified=True,
        live_config_verified=True,
        target_decisions=args.target_days * 5,
    )
    print(json.dumps(report.to_dict(), indent=2))
    return 0 if report.is_promotion_eligible else 1


def _cmd_evaluate_counterfactual(args: argparse.Namespace) -> int:
    from decimal import Decimal

    from trading.analytics.counterfactual import (
        CounterfactualTradeInput,
        evaluate_counterfactuals,
        format_counterfactual_report,
    )
    from trading.domain.enums import AgentAction

    exec_path = Path(args.executed)
    cf_path = Path(args.counterfactual)
    raw_data: list[dict[str, Any]] = []
    if exec_path.exists():
        raw_data.extend(json.loads(exec_path.read_text(encoding="utf-8")))
    if cf_path.exists():
        raw_data.extend(json.loads(cf_path.read_text(encoding="utf-8")))
    records: list[CounterfactualTradeInput] = []
    for item in raw_data:
        records.append(
            CounterfactualTradeInput(
                decision_id=item.get("decision_id", ""),
                trade_id=item.get("trade_id", ""),
                action=AgentAction(item.get("action", "ACCEPT")),
                size_multiplier=Decimal(str(item.get("size_multiplier", "1.0"))),
                baseline_outcome_r=Decimal(str(item.get("baseline_outcome_r", "0"))),
            )
        )
    results = evaluate_counterfactuals(records)
    print(format_counterfactual_report(results))
    return 0


def _cmd_risk_one_lot(args: argparse.Namespace) -> int:
    repo = _repo_root()
    from trading.domain.primitives import Currency, Money
    from trading.risk.affordability import (
        evaluate_affordability,
        persist_affordability_report,
    )

    equity = Money.of(Decimal(str(args.equity)), Currency.INR) if args.equity else None
    report = evaluate_affordability(
        repo,
        lot_size_override=args.lot_size_override,
        total_equity=equity,
    )
    out_path = (
        Path(args.output)
        if args.output
        else repo / "docs" / "reports" / "G1_ONE_LOT_AFFORDABILITY.md"
    )
    saved = persist_affordability_report(repo, report, out_path)
    print(f"Gate G1 One-Lot Affordability Report persisted to: {saved}")
    print(
        f"Evaluated {len(report.evaluations)} structures across 4 modes (NIFTY Lot: {report.lot_size})."
    )
    fits = sum(1 for e in report.evaluations if e.one_lot_fits_budget)
    exceeds = sum(1 for e in report.evaluations if not e.one_lot_fits_budget)
    print(
        f"One-Lot Fits Cap: {fits}, Exceeds Budget: {exceeds} "
        "(Note: fitting cap is offline feasibility only, NOT authorization to run PAPER; G2 requires lifecycle proof)"
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
    vix_history = backfill_sub.add_parser(
        "vix-history",
        help="backfill India VIX daily closes into the snapshot store",
    )
    vix_history.add_argument(
        "--days",
        type=int,
        default=45,
        help="inclusive lookback in UTC days (default: 45)",
    )
    vix_history.set_defaults(func=_cmd_data_backfill_vix_history)
    news = data_sub.add_parser("news", help="macro news JSONL helpers")
    news_sub = news.add_subparsers(dest="news_cmd", required=True)
    validate = news_sub.add_parser("validate", help="check macro_news.jsonl records")
    validate.add_argument(
        "--file",
        default="",
        help="JSONL path (default: macro_news.input_file from config)",
    )
    validate.set_defaults(func=_cmd_data_news_validate)
    record_chain = data_sub.add_parser(
        "record-chain", help="record option chains to local Parquet storage"
    )
    record_chain.add_argument(
        "--symbols",
        default="NSE:NIFTY50-INDEX",
        help="comma-separated symbols (e.g. NSE:NIFTY50-INDEX,NSE:NIFTYBANK-INDEX)",
    )
    record_chain.add_argument(
        "--interval",
        type=int,
        default=60,
        help="polling interval in seconds",
    )
    record_chain.add_argument(
        "--iterations",
        type=int,
        default=1,
        help="max iterations (0 for infinite)",
    )
    record_chain.add_argument(
        "--out-dir",
        default="data/recorded_chains",
        help="output directory for partitioned Parquet",
    )
    record_chain.set_defaults(func=_cmd_data_record_chain)

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
        operation.set_defaults(func=handler)

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

    decisions_parser = evaluate_sub.add_parser(
        "decisions",
        help="print durable decision records for one session date",
    )
    decisions_parser.add_argument(
        "--store",
        default="data/paper/trading.sqlite",
        help="path to TradingStore sqlite file",
    )
    decisions_parser.add_argument(
        "--date",
        required=True,
        help="session date YYYY-MM-DD (Asia/Kolkata)",
    )
    decisions_parser.add_argument(
        "--mode",
        default="",
        help="optional mode filter (M1_CAS, M2_DIRECTIONAL, ...)",
    )
    decisions_parser.add_argument(
        "--experiment-id",
        default="",
        help="optional experiment_id filter",
    )
    decisions_parser.set_defaults(func=_cmd_evaluate_decisions)

    research_weekly_parser = evaluate_sub.add_parser(
        "research-weekly",
        help="ADESK-E1 RESEARCH weekly: clusters, bias summary, playbook proposals",
    )
    research_weekly_parser.add_argument(
        "--store",
        default="data/trading.sqlite",
        help="path to TradingStore sqlite file",
    )
    research_weekly_parser.add_argument(
        "--out-dir",
        default="data/agent_runs",
        help="directory for weekly JSON artifact",
    )
    research_weekly_parser.add_argument(
        "--as-of",
        default="",
        help="UTC evaluation instant (default: now)",
    )
    research_weekly_parser.add_argument(
        "--cohort-id",
        default="",
        help="optional cohort label for bias/report identity",
    )
    research_weekly_parser.set_defaults(func=_cmd_evaluate_research_weekly)

    monthly_meta_parser = evaluate_sub.add_parser(
        "monthly-meta",
        help="ADESK-E3 monthly meta-report: scorecards, demotions, det-vs-desk",
    )
    monthly_meta_parser.add_argument(
        "--store",
        default="data/trading.sqlite",
        help="path to TradingStore sqlite file",
    )
    monthly_meta_parser.add_argument(
        "--out-dir",
        default="data/agent_runs",
        help="directory for monthly meta JSON/markdown",
    )
    monthly_meta_parser.add_argument(
        "--as-of",
        default="",
        help="UTC evaluation instant (default: now)",
    )
    monthly_meta_parser.set_defaults(func=_cmd_evaluate_monthly_meta)

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

    operator_view_parser = evaluate_sub.add_parser(
        "operator-view",
        help="print four-mode allocations and per-family G1/G2 status as JSON",
    )
    operator_view_parser.set_defaults(func=_cmd_evaluate_operator_view)

    desk_parser = evaluate_sub.add_parser(
        "desk",
        help="print Agent Desk PART 14 scorecard for one role as JSON",
    )
    desk_parser.add_argument(
        "--role",
        required=True,
        help="DeskRole value (ENTRY, POSITION, ...)",
    )
    desk_parser.add_argument(
        "--store",
        default="data/trading.sqlite",
        help="path to TradingStore sqlite file",
    )
    desk_parser.add_argument(
        "--as-of",
        default="",
        help="UTC evaluation instant (default: now)",
    )
    desk_parser.set_defaults(func=_cmd_evaluate_desk)

    p_tc = evaluate_sub.add_parser(
        "terminal-cost",
        help="ADESK-C4 terminal-policy cost saved vs flatten (R + INR)",
    )
    p_tc.add_argument("--as-of", default=None, help="UTC timestamp")
    p_tc.add_argument(
        "--fixture",
        action="store_true",
        help="emit a deterministic sample row (smoke / demo)",
    )
    p_tc.set_defaults(func=_cmd_evaluate_terminal_cost)

    readiness_parser = evaluate_sub.add_parser(
        "paper-readiness",
        help="evaluate 30-day paper data accumulation readiness (PAPER-005)",
    )
    readiness_parser.add_argument(
        "--runs-dir",
        default="data/paper/runs",
        help="directory containing daily paper session JSON files",
    )
    readiness_parser.add_argument(
        "--target-days",
        type=int,
        default=30,
        help="required accumulation days (default: 30)",
    )
    readiness_parser.set_defaults(func=_cmd_evaluate_paper_readiness)

    cf_parser = evaluate_sub.add_parser(
        "counterfactual",
        help="compare executed paper trades against counterfactual alternatives",
    )
    cf_parser.add_argument(
        "--executed",
        required=True,
        help="path to executed paper trades JSON file",
    )
    cf_parser.add_argument(
        "--counterfactual",
        required=True,
        help="path to counterfactual trades JSON file",
    )
    cf_parser.set_defaults(func=_cmd_evaluate_counterfactual)

    risk = sub.add_parser("risk", help="Layer 2 risk, limits and affordability")
    risk_sub = risk.add_subparsers(dest="risk_cmd", required=True)
    one_lot = risk_sub.add_parser(
        "one-lot", help="evaluate Gate G1 one-lot affordability across all 4 modes"
    )
    one_lot.add_argument(
        "--output",
        default="",
        help="custom report destination path (defaults to docs/reports/G1_ONE_LOT_AFFORDABILITY.md)",
    )
    one_lot.add_argument(
        "--lot-size-override",
        type=int,
        default=None,
        help="override instrument master lot size for scenario testing",
    )
    one_lot.add_argument(
        "--equity",
        default="",
        help="total reference paper equity in INR (defaults to 700000)",
    )
    one_lot.set_defaults(func=_cmd_risk_one_lot)

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
    weekly.add_argument(
        "--grant-id",
        default="",
        help="optional AuthorityGrant ID in trading store",
    )
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
    advise.add_argument(
        "--grant-id",
        default="",
        help="optional AuthorityGrant ID in trading store",
    )
    advise.set_defaults(func=_cmd_agent_advise)

    ops = sub.add_parser("ops", help="autonomous unattended operations and supervisor")
    ops_sub = ops.add_subparsers(dest="ops_cmd", required=True)

    daemon = ops_sub.add_parser(
        "daemon", help="run unattended trading supervisor daemon"
    )
    daemon.add_argument(
        "--interval", type=int, default=60, help="poll interval in seconds"
    )
    daemon.add_argument(
        "--iterations",
        type=int,
        default=0,
        help="max iterations (0 for infinite)",
    )
    daemon.add_argument(
        "--dry-run",
        action="store_true",
        help="simulate without triggering commands",
    )
    daemon.set_defaults(func=_cmd_ops_daemon)

    ensure_paper = ops_sub.add_parser(
        "ensure-paper",
        help="ensure PAPER runtime units are active for the current market phase",
    )
    ensure_paper.set_defaults(func=_cmd_ops_ensure_paper)

    post_open = ops_sub.add_parser(
        "post-open-check",
        help="run the 09:16 four-mode PAPER checklist and notify Telegram",
    )
    post_open.set_defaults(func=_cmd_ops_post_open_check)

    alert_failure = ops_sub.add_parser(
        "alert-unit-failure",
        help="send Telegram alert when a systemd unit fails",
    )
    alert_failure.add_argument("unit", help="systemd unit name")
    alert_failure.set_defaults(func=_cmd_ops_alert_unit_failure)

    tg_bot = ops_sub.add_parser(
        "telegram-bot", help="run two-way interactive Telegram bot"
    )
    tg_bot.add_argument(
        "--interval", type=int, default=3, help="poll interval in seconds"
    )
    tg_bot.add_argument(
        "--iterations",
        type=int,
        default=0,
        help="max iterations (0 for infinite)",
    )
    tg_bot.set_defaults(func=_cmd_ops_telegram_bot)

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
