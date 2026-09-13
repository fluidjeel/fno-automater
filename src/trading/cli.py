"""Command-line entry points for operations and the data pipeline."""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

from trading.data.config import load_data_pipeline_config
from trading.data.fyers.auth import run_interactive_auth
from trading.data.fyers.ws import FyersTickStream
from trading.data.macro_news import format_macro_summary, load_macro_news_jsonl
from trading.data.pipeline import build_pipeline
from trading.data.replay import build_replay_engine
from trading.data.settings import FyersSettings
from trading.data.storage.parquet_store import JsonlEventStore
from trading.domain.clock import WallClock
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

__all__ = ["main"]


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def _cmd_auth_fyers(args: argparse.Namespace) -> int:
    return run_interactive_auth(
        _repo_root(),
        auth_code=args.auth_code,
        open_browser=not args.no_browser,
    )


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

    args = parser.parse_args(argv)
    func = getattr(args, "func", None)
    if func is None:
        parser.print_help()
        return 1
    return int(func(args))


if __name__ == "__main__":
    raise SystemExit(main())
