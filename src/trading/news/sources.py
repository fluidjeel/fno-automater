"""Configured public-source collectors with bounded retry and circuit control."""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from xml.etree.ElementTree import Element
from zoneinfo import ZoneInfo

import httpx
from defusedxml import ElementTree as SafeElementTree

from trading.news.config import NewsSourceConfig, NewsSubsystemConfig
from trading.news.contracts import NewsItem, NewsQuality

__all__ = [
    "CollectionBatch",
    "NewsCollector",
    "SourceHealth",
    "canonicalize_url",
    "normalize_news_item",
]

_TRACKING_PARAMS = frozenset({"fbclid", "gclid", "mc_cid", "mc_eid"})
_SPACE_RE = re.compile(r"\s+")
_HTTPS_DEFAULT_PORT = 443
_HTTP_TOO_MANY_REQUESTS = 429
_HTTP_SERVER_ERROR = 500
_MAX_BACKFILL_DAYS = 31


@dataclass(frozen=True, slots=True)
class SourceHealth:
    source_id: str
    status: str
    fetched_count: int
    invalid_count: int
    as_of: datetime
    reason: str = ""
    retry_count: int = 0


@dataclass(frozen=True, slots=True)
class CollectionBatch:
    cycle_id: str
    as_of: datetime
    items: tuple[NewsItem, ...]
    source_health: tuple[SourceHealth, ...]


@dataclass(slots=True)
class _Circuit:
    failures: int = 0
    open_until: datetime | None = None


def canonicalize_url(url: str) -> str:
    """Normalize URL identity and drop common tracking parameters."""
    parts = urlsplit(url.strip())
    if parts.scheme.lower() != "https" or not parts.hostname:
        raise ValueError("news URLs must be absolute HTTPS URLs")
    host = parts.hostname.lower()
    if parts.port and parts.port != _HTTPS_DEFAULT_PORT:
        host = f"{host}:{parts.port}"
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in _TRACKING_PARAMS
    ]
    query.sort()
    path = parts.path or "/"
    return urlunsplit(("https", host, path, urlencode(query), ""))


def normalize_news_item(
    *,
    source: NewsSourceConfig,
    url: str,
    headline: str,
    snippet: str,
    language: str,
    published_at: datetime,
    retrieved_at: datetime,
    provider_metadata: dict[str, str | int | bool],
    config: NewsSubsystemConfig,
) -> NewsItem:
    """Create stable IDs and hashes while retaining only permitted snippets."""
    canonical_url = canonicalize_url(url)
    normalized_headline = _SPACE_RE.sub(" ", headline).strip()
    normalized_snippet = _SPACE_RE.sub(" ", snippet).strip()
    if not normalized_headline:
        raise ValueError("news headline is empty")
    normalized_headline = normalized_headline[: config.max_headline_chars]
    normalized_snippet = normalized_snippet[: config.max_snippet_chars]
    content_hash = hashlib.sha256(
        f"{normalized_headline.casefold()}\n{normalized_snippet.casefold()}".encode()
    ).hexdigest()
    title_hash = hashlib.sha256(normalized_headline.casefold().encode()).hexdigest()
    url_hash = hashlib.sha256(canonical_url.encode()).hexdigest()
    item_id = hashlib.sha256(
        f"{source.source_id}\n{canonical_url}\n{content_hash}".encode()
    ).hexdigest()
    quality = NewsQuality.VALID
    if published_at > retrieved_at:
        raise ValueError("publication time is after retrieval time")
    return NewsItem(
        news_item_id=item_id,
        source_id=source.source_id,
        source_tier=source.tier,
        canonical_url=canonical_url,
        url_hash=url_hash,
        title_hash=title_hash,
        headline=normalized_headline,
        snippet=normalized_snippet,
        language=language or source.language,
        published_at=published_at,
        retrieved_at=retrieved_at,
        content_hash=content_hash,
        provider_metadata=provider_metadata,
        quality_state=quality,
    )


_RSS_DATE_FORMATS = (
    "%d %b, %Y %z",
    "%d %b %Y %z",
    "%d %b %Y %H:%M:%S %z",
)


def _parse_rss_date(value: str) -> datetime:
    for fmt in _RSS_DATE_FORMATS:
        try:
            # Every format above ends in %z, so the result is always aware.
            return datetime.strptime(value, fmt)  # noqa: DTZ007
        except ValueError:
            continue
    raise ValueError(f"unrecognized publication time format: {value!r}")


def _aware_datetime(value: str, *, assume_timezone: str = "") -> datetime:
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(text)
        except (ValueError, TypeError):
            parsed = _parse_rss_date(text)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        if assume_timezone:
            parsed = parsed.replace(tzinfo=ZoneInfo(assume_timezone))
        else:
            raise ValueError("publication time has no timezone")
    return parsed.astimezone(UTC)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", maxsplit=1)[-1].lower()


def _xml_text(element: Element, names: set[str]) -> str:
    for node in element.iter():
        if _local_name(node.tag) in names and node.text:
            return node.text.strip()
    return ""


def _rss_items(
    payload: bytes,
    source: NewsSourceConfig,
    retrieved_at: datetime,
    config: NewsSubsystemConfig,
) -> tuple[tuple[NewsItem, ...], int]:
    root = SafeElementTree.fromstring(payload)
    elements = [
        node for node in root.iter() if _local_name(node.tag) in {"item", "entry"}
    ]
    items: list[NewsItem] = []
    invalid = 0
    for element in elements[: source.max_items]:
        try:
            title = _xml_text(element, {"title"})
            snippet = _xml_text(element, {"description", "summary"})
            link = ""
            for child in element:
                if _local_name(child.tag) == "link":
                    link = child.attrib.get("href", child.text or "").strip()
                    if link:
                        break
            published = _xml_text(element, {"pubdate", "published", "updated", "date"})
            if not link or not published:
                raise ValueError("RSS item is missing link or publication time")
            pub_time = _aware_datetime(
                published, assume_timezone=source.assume_timezone
            )
            metadata: dict[str, str | int | bool] = {}
            guid = _xml_text(element, {"guid", "id"})
            if guid:
                metadata["provider_id"] = guid[:512]
            items.append(
                normalize_news_item(
                    source=source,
                    url=link,
                    headline=title,
                    snippet=snippet,
                    language=source.language,
                    published_at=pub_time,
                    retrieved_at=retrieved_at,
                    provider_metadata=metadata,
                    config=config,
                )
            )
        except (ValueError, SafeElementTree.ParseError):
            invalid += 1
    return tuple(items), invalid


def _gdelt_item(
    row: dict[str, object],
    source: NewsSourceConfig,
    retrieved_at: datetime,
    config: NewsSubsystemConfig,
) -> NewsItem:
    stamp = str(row.get("seendate", ""))
    if re.fullmatch(r"\d{8}T\d{6}Z", stamp):
        published_at = datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    else:
        published_at = _aware_datetime(stamp)
    metadata: dict[str, str | int | bool] = {
        key: str(row[key])
        for key in ("domain", "sourceCountry", "sourceCollection")
        if row.get(key) is not None
    }
    return normalize_news_item(
        source=source,
        url=str(row.get("url", "")),
        headline=str(row.get("title", "")),
        snippet="",
        language=str(row.get("language", source.language)),
        published_at=published_at,
        retrieved_at=retrieved_at,
        provider_metadata=metadata,
        config=config,
    )


def _macro_observation_item(
    row: dict[str, object],
    source: NewsSourceConfig,
    retrieved_at: datetime,
    config: NewsSubsystemConfig,
) -> NewsItem:
    series_id = source.params.get("series_id", "")
    period_key = "date" if source.kind == "FRED" else "period"
    period = str(row.get(period_key, ""))
    value = str(row.get("value", "."))
    if not period or not series_id or value == ".":
        raise ValueError("macro observation is missing series, period or value")
    if source.kind == "FRED":
        url = f"https://fred.stlouisfed.org/series/{series_id}"
        provider = "FRED"
        period_label = "observation date"
    else:
        url = source.endpoint
        provider = "EIA"
        period_label = "observation period"
    metadata: dict[str, str | int | bool] = {
        "series_id": series_id,
        "observation_period": period,
        "timestamp_semantics": "retrieval time; provider gives no release timestamp",
    }
    return normalize_news_item(
        source=source,
        url=url,
        headline=f"{provider} {series_id} observation",
        snippet=f"{period_label.title()} {period}; value {value}.",
        language=source.language,
        published_at=retrieved_at,
        retrieved_at=retrieved_at,
        provider_metadata=metadata,
        config=config,
    )


def _json_items(
    payload: bytes,
    source: NewsSourceConfig,
    retrieved_at: datetime,
    config: NewsSubsystemConfig,
) -> tuple[tuple[NewsItem, ...], int]:
    decoded = json.loads(payload)
    if source.kind == "GDELT":
        rows = decoded.get("articles", [])
    elif source.kind == "FRED":
        rows = decoded.get("observations", [])
    elif source.kind == "EIA":
        rows = decoded.get("response", {}).get("data", [])
    else:
        rows = []
    items: list[NewsItem] = []
    invalid = 0
    for row in rows[: source.max_items]:
        if not isinstance(row, dict):
            invalid += 1
            continue
        try:
            if source.kind == "GDELT":
                item = _gdelt_item(row, source, retrieved_at, config)
            else:
                item = _macro_observation_item(row, source, retrieved_at, config)
            items.append(item)
        except (ValueError, TypeError, KeyError):
            invalid += 1
    return tuple(items), invalid


@dataclass(slots=True)
class NewsCollector:
    """Collect enabled sources without making them dependencies of trading."""

    config: NewsSubsystemConfig
    timeout_seconds: float = 10.0
    max_attempts: int = 3
    sleep_fn: Callable[[float], None] = time.sleep
    jitter_fn: Callable[[], float] = random.random
    transport: httpx.BaseTransport | None = None
    _circuits: dict[str, _Circuit] = field(default_factory=dict)
    _last_attempt: dict[str, datetime] = field(default_factory=dict)

    def collect(
        self,
        *,
        as_of: datetime,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
    ) -> CollectionBatch:
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("collector as_of must be timezone-aware")
        instant = as_of.astimezone(UTC)
        if (start_at is None) != (end_at is None):
            raise ValueError("backfill requires both start_at and end_at")
        if start_at is not None and end_at is not None:
            if start_at.tzinfo is None or end_at.tzinfo is None:
                raise ValueError("backfill bounds must be timezone-aware")
            start_at, end_at = start_at.astimezone(UTC), end_at.astimezone(UTC)
            if start_at > end_at or end_at > instant:
                raise ValueError(
                    "backfill bounds must be ordered and not in the future"
                )
            if (end_at - start_at).days > _MAX_BACKFILL_DAYS:
                raise ValueError("backfill range cannot exceed 31 days")
        collected: list[NewsItem] = []
        health: list[SourceHealth] = []
        for source in self.config.sources:
            if not source.enabled:
                health.append(SourceHealth(source.source_id, "DISABLED", 0, 0, instant))
                continue
            circuit = self._circuits.setdefault(source.source_id, _Circuit())
            if circuit.open_until is not None and instant < circuit.open_until:
                health.append(
                    SourceHealth(
                        source.source_id,
                        "CIRCUIT_OPEN",
                        0,
                        0,
                        instant,
                        "cooldown active",
                    )
                )
                continue
            previous = self._last_attempt.get(source.source_id)
            if (
                previous is not None
                and (instant - previous).total_seconds() < source.interval_seconds
            ):
                health.append(
                    SourceHealth(
                        source.source_id, "RATE_LIMITED", 0, 0, instant, "poll interval"
                    )
                )
                continue
            self._last_attempt[source.source_id] = instant
            if source.kind in {"FRED", "EIA"}:
                key = (
                    os.environ.get(source.api_key_env, "") if source.api_key_env else ""
                )
                if not key:
                    health.append(
                        SourceHealth(
                            source.source_id,
                            "DISABLED_NO_KEY",
                            0,
                            0,
                            instant,
                            f"environment variable {source.api_key_env} is not set",
                        )
                    )
                    continue
            items, invalid, retries, error = self._collect_source(
                source, instant, start_at=start_at, end_at=end_at
            )
            if start_at is not None and end_at is not None:
                items = tuple(
                    item for item in items if start_at <= item.published_at <= end_at
                )
            collected.extend(items)
            if error:
                circuit.failures += 1
                if circuit.failures >= source.circuit_failure_threshold:
                    circuit.open_until = instant + timedelta(
                        seconds=source.circuit_cooldown_seconds
                    )
                status = "PARTIAL" if items else "ERROR"
                health.append(
                    SourceHealth(
                        source.source_id,
                        status,
                        len(items),
                        invalid,
                        instant,
                        error,
                        retries,
                    )
                )
            else:
                circuit.failures = 0
                circuit.open_until = None
                health.append(
                    SourceHealth(
                        source.source_id,
                        "OK",
                        len(items),
                        invalid,
                        instant,
                        "",
                        retries,
                    )
                )
        cycle_seed = "\n".join(
            [
                instant.isoformat(),
                *(
                    item.news_item_id
                    for item in sorted(collected, key=lambda i: i.news_item_id)
                ),
            ]
        )
        cycle_id = hashlib.sha256(cycle_seed.encode()).hexdigest()
        return CollectionBatch(cycle_id, instant, tuple(collected), tuple(health))

    def _collect_source(
        self,
        source: NewsSourceConfig,
        as_of: datetime,
        *,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
    ) -> tuple[tuple[NewsItem, ...], int, int, str]:
        params: dict[str, str | int] = dict(source.params)
        if source.kind == "GDELT":
            params.update(
                {
                    "query": source.query,
                    "mode": "artlist",
                    "format": "json",
                    "maxrecords": source.max_items,
                    "sort": "datedesc",
                }
            )
            if start_at is not None and end_at is not None:
                params["startdatetime"] = start_at.strftime("%Y%m%d%H%M%S")
                params["enddatetime"] = end_at.strftime("%Y%m%d%H%M%S")
        elif source.kind == "FRED":
            params["file_type"] = "json"
            params["output_type"] = "4"
            params.setdefault(
                "observation_start", (as_of - timedelta(days=14)).date().isoformat()
            )
            params.setdefault("observation_end", as_of.date().isoformat())
            if start_at is not None and end_at is not None:
                params["observation_start"] = start_at.date().isoformat()
                params["observation_end"] = end_at.date().isoformat()
            api_key = os.environ.get(source.api_key_env, "")
            params["api_key"] = api_key
        elif source.kind == "EIA":
            params["api_key"] = os.environ.get(source.api_key_env, "")
        attempts = min(source.retry_attempts, self.max_attempts)
        last_error = "source request failed"
        for attempt in range(1, attempts + 1):
            try:
                with httpx.Client(
                    timeout=min(source.timeout_seconds, self.timeout_seconds),
                    transport=self.transport,
                    follow_redirects=True,
                ) as client:
                    response = client.get(source.endpoint, params=params)
                if (
                    response.status_code == _HTTP_TOO_MANY_REQUESTS
                    or response.status_code >= _HTTP_SERVER_ERROR
                ):
                    raise httpx.HTTPStatusError(
                        f"source HTTP {response.status_code}",
                        request=response.request,
                        response=response,
                    )
                response.raise_for_status()
                if len(response.content) > source.max_payload_bytes:
                    raise ValueError("source payload exceeds configured size limit")
                if source.kind in {"RSS", "GOOGLE_NEWS_RSS"}:
                    items, invalid = _rss_items(
                        response.content, source, as_of, self.config
                    )
                else:
                    items, invalid = _json_items(
                        response.content, source, as_of, self.config
                    )
                return (
                    items,
                    invalid,
                    attempt - 1,
                    "" if not invalid else f"{invalid} invalid item(s)",
                )
            except (
                httpx.HTTPError,
                ValueError,
                json.JSONDecodeError,
                SafeElementTree.ParseError,
            ) as exc:
                last_error = (
                    f"HTTP {exc.response.status_code}"
                    if isinstance(exc, httpx.HTTPStatusError)
                    else type(exc).__name__
                )
                retryable = isinstance(exc, httpx.TransportError) or (
                    isinstance(exc, httpx.HTTPStatusError)
                    and (
                        exc.response.status_code in {408, _HTTP_TOO_MANY_REQUESTS}
                        or exc.response.status_code >= _HTTP_SERVER_ERROR
                    )
                )
                if not retryable:
                    break
                if attempt < attempts:
                    delay = min(30.0, 0.5 * (2 ** (attempt - 1)))
                    self.sleep_fn(delay * (0.5 + self.jitter_fn()))
        return (), 0, attempts - 1, last_error
