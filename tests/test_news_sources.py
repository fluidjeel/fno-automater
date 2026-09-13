"""Offline adapter tests for public news and macro sources."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from trading.news.config import NewsSourceConfig, load_news_config
from trading.news.sources import NewsCollector, canonicalize_url

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG = load_news_config(REPO_ROOT / "config" / "news.yaml")
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "news"
NOW = datetime(2026, 9, 13, 3, 0, tzinfo=UTC)


def _collector(
    source: NewsSourceConfig,
    transport: httpx.BaseTransport,
    *,
    sleep_fn: Callable[[float], None] | None = None,
    jitter_fn: Callable[[], float] | None = None,
) -> NewsCollector:
    args: dict[str, object] = {
        "config": CONFIG.model_copy(update={"sources": (source,)}),
        "transport": transport,
    }
    if sleep_fn is not None:
        args["sleep_fn"] = sleep_fn
    if jitter_fn is not None:
        args["jitter_fn"] = jitter_fn
    return NewsCollector(**args)  # type: ignore[arg-type]


def test_canonicalize_url_removes_tracking_params_and_fragment() -> None:
    assert (
        canonicalize_url("https://Example.com/story?utm_source=rss&id=3#section")
        == "https://example.com/story?id=3"
    )


def test_rss_collector_normalizes_only_headline_and_snippet() -> None:
    source = next(
        item for item in CONFIG.sources if item.source_id == "rbi_press_releases"
    )
    body = (FIXTURES / "rbi_press_releases.xml").read_bytes()
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=body))
    batch = _collector(source, transport).collect(as_of=NOW)
    assert len(batch.items) == 1
    item = batch.items[0]
    assert item.headline == "RBI policy update"
    assert item.canonical_url == "https://rbi.org.in/story"
    assert item.snippet == "Permitted summary only"
    assert item.published_at == NOW + timedelta(minutes=-5)
    assert batch.source_health[0].status == "OK"


def test_rss_collector_applies_configured_timezone_to_naive_dates() -> None:
    source = next(
        item for item in CONFIG.sources if item.source_id == "rbi_press_releases"
    )
    body = (
        b'<?xml version="1.0"?><rss><channel><item>'
        b"<title>Naive dated item</title>"
        b"<link>https://rbi.org.in/scripts/BS_PressReleaseDisplay.aspx?prid=1</link>"
        b"<description>summary</description>"
        b"<pubDate>Fri, 11 Sep 2026 21:40:00</pubDate>"
        b"</item></channel></rss>"
    )
    batch = _collector(
        source, httpx.MockTransport(lambda request: httpx.Response(200, content=body))
    ).collect(as_of=NOW)
    assert len(batch.items) == 1
    assert batch.items[0].published_at == datetime(2026, 9, 11, 16, 10, tzinfo=UTC)
    assert batch.source_health[0].status == "OK"


def test_rss_collector_parses_sebi_day_month_year_format() -> None:
    source = next(item for item in CONFIG.sources if item.source_id == "sebi_rss")
    body = (
        b'<?xml version="1.0"?><rss><channel><item>'
        b"<title>SEBI order</title>"
        b"<link>https://www.sebi.gov.in/enforcement/orders/example</link>"
        b"<description>summary</description>"
        b"<pubDate>11 Sep, 2026 +0530</pubDate>"
        b"</item></channel></rss>"
    )
    batch = _collector(
        source, httpx.MockTransport(lambda request: httpx.Response(200, content=body))
    ).collect(as_of=NOW)
    assert len(batch.items) == 1
    assert batch.items[0].published_at == datetime(2026, 9, 10, 18, 30, tzinfo=UTC)
    assert batch.source_health[0].status == "OK"


def test_gdelt_collector_uses_bounded_json_article_list() -> None:
    source = next(item for item in CONFIG.sources if item.source_id == "gdelt_global")
    body = (FIXTURES / "gdelt_articles.json").read_bytes()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=body)

    batch = _collector(source, httpx.MockTransport(handler)).collect(as_of=NOW)
    assert len(batch.items) == 1
    assert batch.items[0].published_at == NOW - timedelta(minutes=5)
    assert "maxrecords=100" in str(requests[0].url)
    assert requests[0].url.params.get("format") == "json"


def test_backfill_passes_explicit_utc_range_and_rejects_unbounded_ranges() -> None:
    source = next(item for item in CONFIG.sources if item.source_id == "gdelt_global")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=b'{"articles":[]}')

    collector = _collector(source, httpx.MockTransport(handler))
    collector.collect(
        as_of=NOW,
        start_at=NOW - timedelta(days=2),
        end_at=NOW - timedelta(days=1),
    )
    assert requests[0].url.params.get("startdatetime") == "20260911030000"
    assert requests[0].url.params.get("enddatetime") == "20260912030000"
    with pytest.raises(ValueError, match="31 days"):
        _collector(source, httpx.MockTransport(handler)).collect(
            as_of=NOW,
            start_at=NOW - timedelta(days=33),
            end_at=NOW - timedelta(days=1),
        )


def test_transient_failure_retries_with_bounded_backoff_and_partial_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = next(item for item in CONFIG.sources if item.source_id == "gdelt_global")
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            return httpx.Response(503)
        return httpx.Response(200, content=b'{"articles":[]}')

    delays: list[float] = []
    collector = _collector(
        source,
        httpx.MockTransport(handler),
        sleep_fn=delays.append,
        jitter_fn=lambda: 0.5,
    )
    batch = collector.collect(as_of=NOW)
    assert calls == 3
    assert batch.source_health[0].status == "OK"
    assert batch.source_health[0].retry_count == 2
    assert delays == [0.5, 1.0]


def test_malformed_feed_is_reported_without_repeating_the_same_response() -> None:
    source = next(
        item for item in CONFIG.sources if item.source_id == "rbi_press_releases"
    )
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=b"<rss><broken></rss>")

    delays: list[float] = []
    batch = _collector(
        source,
        httpx.MockTransport(handler),
        sleep_fn=delays.append,
    ).collect(as_of=NOW)
    assert calls == 1
    assert delays == []
    assert batch.source_health[0].status == "ERROR"


def test_keyed_sources_are_safe_without_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fred = next(item for item in CONFIG.sources if item.source_id == "fred_indicators")
    eia = next(item for item in CONFIG.sources if item.source_id == "eia_energy")
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    monkeypatch.delenv("EIA_API_KEY", raising=False)
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={})

    config = CONFIG.model_copy(update={"sources": (fred, eia)})
    batch = NewsCollector(config, transport=httpx.MockTransport(handler)).collect(
        as_of=NOW
    )
    assert [health.status for health in batch.source_health] == [
        "DISABLED_NO_KEY",
        "DISABLED",
    ]
    assert calls == []


def test_source_interval_and_circuit_breaker_are_observable() -> None:
    source = CONFIG.sources[0].model_copy(
        update={
            "interval_seconds": 1,
            "circuit_failure_threshold": 1,
            "circuit_cooldown_seconds": 30,
        }
    )
    collector = _collector(
        source,
        httpx.MockTransport(lambda request: httpx.Response(503)),
        sleep_fn=lambda _: None,
    )
    first = collector.collect(as_of=NOW)
    second = collector.collect(as_of=NOW + timedelta(seconds=2))
    third = collector.collect(as_of=NOW + timedelta(seconds=3))
    assert first.source_health[0].status == "ERROR"
    assert second.source_health[0].status == "CIRCUIT_OPEN"
    assert third.source_health[0].status == "CIRCUIT_OPEN"
