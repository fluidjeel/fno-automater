"""Read-only FYERS data/TBT WebSocket capability probe helpers."""

from __future__ import annotations

import json
import statistics
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from trading.data.fyers.client import FyersMarketFeed
from trading.data.settings import FyersSettings

__all__ = [
    "CapturedMessage",
    "ProbeSymbolSet",
    "ProbeSummary",
    "analyze_samples",
    "check_tbt_entitlement",
    "collect_data_ws",
    "collect_tbt_ws",
    "depth_level_count",
    "documented_feed_catalog",
    "generate_markdown_report",
    "resolve_probe_symbols",
    "serialize_depth",
]

# Documented FYERS limits (verify against current official docs before production).
DATA_WS_SYMBOL_LIMIT = 5000
TBT_SYMBOL_LIMIT_PER_CONNECTION = 5
TBT_CHANNEL_RANGE = (1, 50)
TBT_CONNECTIONS_PER_APP = 3


@dataclass(frozen=True, slots=True)
class ProbeSymbolSet:
    """Symbols requested for the bounded capability probe."""

    nifty_index: str = "NSE:NIFTY50-INDEX"
    nifty_option: str | None = None
    stock_option: str | None = None
    mcx_gold: str = "MCX:GOLDM"
    mcx_crude: str = "MCX:CRUDEOILM"

    def all_symbols(self) -> tuple[str, ...]:
        symbols = [self.nifty_index, self.mcx_gold, self.mcx_crude]
        if self.nifty_option:
            symbols.append(self.nifty_option)
        if self.stock_option:
            symbols.append(self.stock_option)
        return tuple(dict.fromkeys(symbols))

    def tbt_eligible(self) -> tuple[str, ...]:
        """TBT depth is documented as NSE/NFO only."""
        eligible: list[str] = []
        for symbol in self.all_symbols():
            if symbol.startswith(("NSE:", "NFO:")):
                eligible.append(symbol)
        return tuple(eligible[:TBT_SYMBOL_LIMIT_PER_CONNECTION])


@dataclass(frozen=True, slots=True)
class CapturedMessage:
    """One raw websocket payload with local receipt metadata."""

    feed: str
    data_type: str
    symbol: str | None
    receive_time: datetime
    payload: dict[str, Any]
    message_kind: str = "data"


@dataclass(frozen=True, slots=True)
class ProbeSummary:
    """Aggregated probe findings for one feed/symbol combination."""

    feed: str
    data_type: str
    symbol: str
    message_count: int
    field_keys: tuple[str, ...]
    bid_levels: int | None
    ask_levels: int | None
    has_ltp: bool
    has_last_traded_qty: bool
    has_exchange_timestamp: bool
    has_aggressor_side: bool
    update_mode: str
    sequence_gaps: int
    duplicate_count: int
    median_latency_ms: float | None
    min_interval_ms: float | None
    max_interval_ms: float | None
    errors: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


def resolve_probe_symbols(
    feed: FyersMarketFeed | None = None,
    *,
    stock_underlying: str = "NSE:RELIANCE-EQ",
    fallback: ProbeSymbolSet | None = None,
) -> ProbeSymbolSet:
    """Resolve liquid option symbols via REST; use fallback when REST is unavailable."""
    base = fallback or ProbeSymbolSet()
    if feed is None:
        return base
    nifty_option = base.nifty_option
    stock_option = base.stock_option
    mcx_gold = base.mcx_gold
    mcx_crude = base.mcx_crude
    try:
        nifty_option = _pick_liquid_option(feed.fetch_option_chain("NSE:NIFTY50-INDEX"))
    except Exception:
        pass
    try:
        stock_option = _pick_liquid_option(feed.fetch_option_chain(stock_underlying))
    except Exception:
        pass
    try:
        mcx_gold = _resolve_mcx_symbol(feed, base.mcx_gold)
        mcx_crude = _resolve_mcx_symbol(feed, base.mcx_crude)
    except Exception:
        pass
    return ProbeSymbolSet(
        nifty_option=nifty_option,
        stock_option=stock_option,
        mcx_gold=mcx_gold,
        mcx_crude=mcx_crude,
    )


def documented_feed_catalog() -> dict[str, Any]:
    """SDK-documented fields and limits when live capture is unavailable."""
    return {
        "data_ws": {
            "url": "wss://socket.fyers.in/hsm/v1-5/prod",
            "data_types": {
                "SymbolUpdate": {
                    "message_types": ("if", "sf"),
                    "index_fields": (
                        "ltp",
                        "prev_close_price",
                        "exch_feed_time",
                        "high_price",
                        "low_price",
                        "open_price",
                        "ch",
                        "chp",
                        "type",
                        "symbol",
                    ),
                    "scrip_fields": (
                        "ltp",
                        "vol_traded_today",
                        "last_traded_time",
                        "exch_feed_time",
                        "bid_size",
                        "ask_size",
                        "bid_price",
                        "ask_price",
                        "last_traded_qty",
                        "tot_buy_qty",
                        "tot_sell_qty",
                        "avg_trade_price",
                        "low_price",
                        "high_price",
                        "open_price",
                        "prev_close_price",
                        "ch",
                        "chp",
                        "type",
                        "symbol",
                    ),
                    "depth_levels": 0,
                    "trade_tape": False,
                    "aggressor_side": False,
                    "update_semantics": "full snapshot on subscribe, then field-level deltas (type 85)",
                    "sequence_numbers": "ack_count only; no per-tick sequence in decoded dict",
                    "historical": False,
                },
                "DepthUpdate": {
                    "message_types": ("dp",),
                    "fields": (
                        "bid_price1..5",
                        "ask_price1..5",
                        "bid_size1..5",
                        "ask_size1..5",
                        "bid_order1..5",
                        "ask_order1..5",
                        "type",
                        "symbol",
                    ),
                    "depth_levels": 5,
                    "index_supported": False,
                    "trade_tape": False,
                    "aggressor_side": False,
                    "update_semantics": "full snapshot on subscribe, then field-level deltas",
                    "sequence_numbers": "ack_count only",
                    "historical": False,
                },
            },
            "symbol_limit": DATA_WS_SYMBOL_LIMIT,
            "channels": "1-30 per SDK channel map",
        },
        "tbt_ws": {
            "bootstrap_url": "https://api-t1.fyers.in/indus/home/tbtws",
            "fallback_url": "wss://rtsocket-api.fyers.in/versova",
            "wire_format": "protobuf SocketMessage",
            "mode": "depth",
            "depth_levels": 50,
            "fields": (
                "tbq",
                "tsq",
                "bid_levels[50].price/qty/orders",
                "ask_levels[50].price/qty/orders",
                "feed_time",
                "send_time",
                "sequence_no",
                "snapshot",
                "ticker",
            ),
            "quote_fields_in_proto": (
                "ltp",
                "ltt",
                "ltq",
                "vtt",
                "oi",
            ),
            "trade_tape": False,
            "aggressor_side": False,
            "segments": ("NSE", "NFO"),
            "mcx_supported": False,
            "symbol_limit_per_connection": TBT_SYMBOL_LIMIT_PER_CONNECTION,
            "channels": f"{TBT_CHANNEL_RANGE[0]}-{TBT_CHANNEL_RANGE[1]}",
            "connections_per_app": TBT_CONNECTIONS_PER_APP,
            "update_semantics": "first packet snapshot, subsequent diffs",
            "historical": False,
            "entitlement": "premium add-on; bootstrap returns 403 when not entitled",
        },
        "rest_depth": {
            "endpoint": "/data/depth",
            "depth_levels": 5,
            "fields": ("bids[].price/volume/ord", "ask[].price/volume/ord", "totalbuyqty", "totalsellqty", "ltp", "oi"),
            "historical": False,
        },
    }


def check_tbt_entitlement(settings: FyersSettings) -> dict[str, Any]:
    """Query the TBT bootstrap endpoint; 403 usually means no entitlement."""
    url = "https://api-t1.fyers.in/indus/home/tbtws"
    headers = {"Authorization": settings.auth_header}
    with httpx.Client(timeout=15.0) as client:
        response = client.get(url, headers=headers)
    body: dict[str, Any]
    try:
        body = response.json()
    except json.JSONDecodeError:
        body = {"raw": response.text[:500]}
    return {
        "http_status": response.status_code,
        "entitled": response.status_code == 200 and body.get("s") == "ok",
        "socket_url": body.get("data", {}).get("socket_url")
        if isinstance(body.get("data"), dict)
        else None,
        "message": body.get("message"),
        "body": body,
    }


def collect_data_ws(
    settings: FyersSettings,
    symbols: Sequence[str],
    *,
    data_type: str,
    channel: int = 11,
    duration_seconds: float = 20.0,
    max_messages: int = 100,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[tuple[CapturedMessage, ...], tuple[str, ...]]:
    """Subscribe on the standard data socket and capture raw decoded messages."""
    from fyers_apiv3.FyersWebsocket import data_ws

    captured: list[CapturedMessage] = []
    errors: list[str] = []
    halt = threading.Event()
    deadline = time.monotonic() + duration_seconds

    def on_message(message: dict[str, Any]) -> None:
        if halt.is_set():
            return
        receive_time = datetime.now(tz=UTC)
        if message.get("s") == "error" and message.get("type") in {"cn", "AUTH"}:
            errors.append(str(message))
            halt.set()
            return
        symbol = message.get("symbol")
        if isinstance(symbol, str):
            symbol_key = symbol
        else:
            symbol_key = None
        captured.append(
            CapturedMessage(
                feed="data_ws",
                data_type=data_type,
                symbol=symbol_key,
                receive_time=receive_time,
                payload=dict(message),
                message_kind=_classify_data_ws_message(message),
            )
        )
        if len(captured) >= max_messages:
            halt.set()

    def on_error(message: Any) -> None:
        errors.append(str(message))
        halt.set()

    log_dir = Path("data/logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    socket = data_ws.FyersDataSocket(
        access_token=settings.auth_header,
        write_to_file=False,
        log_path=str(log_dir),
        reconnect=False,
        on_message=on_message,
        on_error=on_error,
    )
    try:
        socket.connect()
        socket.subscribe(symbols=list(symbols), data_type=data_type, channel=channel)
        socket.keep_running()
        while not halt.is_set() and time.monotonic() < deadline:
            sleep(0.1)
    finally:
        halt.set()
        socket.close_connection()
    return tuple(captured), tuple(errors)


def collect_tbt_ws(
    settings: FyersSettings,
    symbols: Sequence[str],
    *,
    channel: str = "1",
    duration_seconds: float = 20.0,
    max_messages: int = 100,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[tuple[CapturedMessage, ...], tuple[str, ...]]:
    """Subscribe on the TBT socket and capture protobuf-decoded depth updates."""
    from fyers_apiv3.FyersWebsocket.tbt_ws import (
        FyersTbtSocket,
        SubscriptionModes,
    )

    captured: list[CapturedMessage] = []
    errors: list[str] = []
    halt = threading.Event()
    deadline = time.monotonic() + duration_seconds
    connected = threading.Event()

    def on_depth_update(ticker: str, depth: Any) -> None:
        if halt.is_set():
            return
        receive_time = datetime.now(tz=UTC)
        payload = serialize_depth(depth)
        captured.append(
            CapturedMessage(
                feed="tbt_ws",
                data_type="Depth",
                symbol=ticker,
                receive_time=receive_time,
                payload=payload,
                message_kind="snapshot" if payload.get("snapshot") else "delta",
            )
        )
        if len(captured) >= max_messages:
            halt.set()

    def on_error_message(message: str) -> None:
        errors.append(message)

    def on_open() -> None:
        connected.set()
        socket.subscribe(set(symbols), channel, SubscriptionModes.DEPTH)
        socket.switchChannel({channel}, set())

    socket = FyersTbtSocket(
        access_token=settings.auth_header,
        write_to_file=False,
        on_depth_update=on_depth_update,
        on_error_message=on_error_message,
        on_open=on_open,
        reconnect=False,
        reconnect_retry=1,
    )
    try:
        socket.connect()
        if not connected.wait(timeout=5.0):
            errors.append("tbt_ws: on_open did not fire within 5s")
        socket.keep_running()
        while not halt.is_set() and time.monotonic() < deadline:
            sleep(0.1)
    finally:
        halt.set()
        socket.stop_running()
        socket.close_connection()
    return tuple(captured), tuple(errors)


def serialize_depth(depth: Any) -> dict[str, Any]:
    """Convert the SDK Depth object into a JSON-serializable dict."""
    bid_levels = [
        {"price": depth.bidprice[i], "qty": depth.bidqty[i], "orders": depth.bidordn[i]}
        for i in range(50)
        if depth.bidprice[i] or depth.bidqty[i] or depth.bidordn[i]
    ]
    ask_levels = [
        {"price": depth.askprice[i], "qty": depth.askqty[i], "orders": depth.askordn[i]}
        for i in range(50)
        if depth.askprice[i] or depth.askqty[i] or depth.askordn[i]
    ]
    return {
        "tbq": depth.tbq,
        "tsq": depth.tsq,
        "bid_levels": bid_levels,
        "ask_levels": ask_levels,
        "snapshot": depth.snapshot,
        "exchange_timestamp": depth.timestamp,
        "send_timestamp": depth.sendtime,
        "sequence_no": depth.seqNo,
    }


def depth_level_count(payload: Mapping[str, Any]) -> tuple[int | None, int | None]:
    """Count bid/ask levels from a normalized or raw payload."""
    if "bid_levels" in payload and "ask_levels" in payload:
        bids = payload["bid_levels"]
        asks = payload["ask_levels"]
        return (
            len(bids) if isinstance(bids, list) else None,
            len(asks) if isinstance(asks, list) else None,
        )
    bid_keys = [key for key in payload if str(key).startswith("bid_price")]
    ask_keys = [key for key in payload if str(key).startswith("ask_price")]
    if bid_keys or ask_keys:
        return (len(bid_keys), len(ask_keys))
    return (None, None)


def analyze_samples(
    samples: Sequence[CapturedMessage],
    *,
    feed: str,
    data_type: str,
    symbol: str,
) -> ProbeSummary:
    """Derive capability metrics from captured raw messages for one symbol."""
    symbol_samples = [
        sample
        for sample in samples
        if sample.symbol == symbol or (sample.symbol is None and len(samples) == 1)
    ]
    if not symbol_samples and samples:
        symbol_samples = list(samples)
    field_keys = _union_keys(sample.payload for sample in symbol_samples)
    bid_levels: list[int] = []
    ask_levels: list[int] = []
    for sample in symbol_samples:
        bid, ask = depth_level_count(sample.payload)
        if bid is not None:
            bid_levels.append(bid)
        if ask is not None:
            ask_levels.append(ask)
    has_ltp = any(
        key in sample.payload
        for sample in symbol_samples
        for key in ("ltp", "last_price", "lp")
    )
    has_last_traded_qty = any(
        key in sample.payload
        for sample in symbol_samples
        for key in ("last_traded_qty", "ltq", "last_qty")
    )
    has_exchange_timestamp = any(
        key in sample.payload
        for sample in symbol_samples
        for key in (
            "exch_feed_time",
            "last_traded_time",
            "exchange_timestamp",
            "timestamp",
            "send_timestamp",
        )
    )
    aggressor_keys = {
        "aggressor",
        "aggressor_side",
        "trade_side",
        "buyer_initiated",
        "seller_initiated",
    }
    has_aggressor_side = any(
        key in sample.payload for sample in symbol_samples for key in aggressor_keys
    )
    kinds = {sample.message_kind for sample in symbol_samples}
    if kinds == {"snapshot"}:
        update_mode = "snapshot_only"
    elif kinds == {"delta"}:
        update_mode = "delta_only"
    elif kinds >= {"snapshot", "delta"}:
        update_mode = "snapshot_then_delta"
    elif not symbol_samples:
        update_mode = "no_data"
    else:
        update_mode = "unknown"
    sequence_gaps, duplicate_count = _sequence_stats(symbol_samples)
    latencies = _latency_ms(symbol_samples)
    median_latency = statistics.median(latencies) if latencies else None
    intervals = _inter_arrival_ms(symbol_samples)
    notes: list[str] = []
    if not has_aggressor_side:
        notes.append(
            "aggressor_side unavailable; trade-initiator could only be approximated "
            "by comparing trade price to contemporaneous best bid/ask (tick rule), "
            "not from depth alone"
        )
    return ProbeSummary(
        feed=feed,
        data_type=data_type,
        symbol=symbol,
        message_count=len(symbol_samples),
        field_keys=field_keys,
        bid_levels=max(bid_levels) if bid_levels else None,
        ask_levels=max(ask_levels) if ask_levels else None,
        has_ltp=has_ltp,
        has_last_traded_qty=has_last_traded_qty,
        has_exchange_timestamp=has_exchange_timestamp,
        has_aggressor_side=has_aggressor_side,
        update_mode=update_mode,
        sequence_gaps=sequence_gaps,
        duplicate_count=duplicate_count,
        median_latency_ms=median_latency,
        min_interval_ms=min(intervals) if intervals else None,
        max_interval_ms=max(intervals) if intervals else None,
        notes=tuple(notes),
    )


def generate_markdown_report(
    *,
    symbols: ProbeSymbolSet,
    entitlement: dict[str, Any],
    data_symbol_updates: Sequence[CapturedMessage],
    data_depth_updates: Sequence[CapturedMessage],
    tbt_updates: Sequence[CapturedMessage],
    data_errors: Mapping[str, Sequence[str]],
    tbt_errors: Sequence[str],
    generated_at: datetime,
) -> str:
    """Render the audit report requested by the capability probe."""
    lines: list[str] = [
        "# FYERS API V3 TBT / Data WebSocket Capability Audit",
        "",
        f"Generated: {generated_at.isoformat()}",
        "",
        "## Probe symbols",
        "",
        f"- NIFTY index: `{symbols.nifty_index}`",
        f"- NIFTY option: `{symbols.nifty_option}`",
        f"- Stock option: `{symbols.stock_option}`",
        f"- MCX gold: `{symbols.mcx_gold}`",
        f"- MCX crude: `{symbols.mcx_crude}`",
        "",
        "## TBT entitlement",
        "",
        f"- HTTP status: {entitlement.get('http_status')}",
        f"- Entitled: {entitlement.get('entitled')}",
        f"- Socket URL: `{entitlement.get('socket_url')}`",
        f"- Message: {entitlement.get('message')}",
        "",
        "## Provider capability matrix",
        "",
        "| Feed | Data type | Symbol class | Depth levels | LTP | LTQ | Exchange ts | "
        "Aggressor | Update mode | Messages |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    summaries = _all_summaries(
        symbols,
        data_symbol_updates,
        data_depth_updates,
        tbt_updates,
    )
    for summary in summaries:
        depth = (
            f"{summary.bid_levels or 0}/{summary.ask_levels or 0}"
            if summary.bid_levels is not None
            else "n/a"
        )
        lines.append(
            f"| {summary.feed} | {summary.data_type} | `{summary.symbol}` | {depth} | "
            f"{'yes' if summary.has_ltp else 'no'} | "
            f"{'yes' if summary.has_last_traded_qty else 'no'} | "
            f"{'yes' if summary.has_exchange_timestamp else 'no'} | "
            f"{'yes' if summary.has_aggressor_side else 'no'} | "
            f"{summary.update_mode} | {summary.message_count} |"
        )
    catalog = documented_feed_catalog()
    lines.extend(["", "## Documented/SDK field catalog", ""])
    lines.append("```json")
    lines.append(json.dumps(catalog, indent=2)[:6000])
    lines.append("```")
    lines.append("")
    lines.extend(["", "## Sample payload schema", ""])
    live_examples = _example_payloads(
        data_symbol_updates, data_depth_updates, tbt_updates
    )
    if live_examples:
        for label, sample in live_examples:
            lines.append(f"### {label}")
            lines.append("")
            lines.append("```json")
            lines.append(json.dumps(sample, indent=2, default=str)[:4000])
            lines.append("```")
            lines.append("")
    else:
        lines.append("No live payloads captured in this run. Documented examples:")
        lines.append("")
        lines.append("### data_ws SymbolUpdate (scrip)")
        lines.append("")
        lines.append("```json")
        lines.append(
            json.dumps(
                {
                    "ltp": 120.25,
                    "bid_price": 120.0,
                    "ask_price": 120.5,
                    "bid_size": 1500,
                    "ask_size": 1200,
                    "last_traded_qty": 50,
                    "vol_traded_today": 125000,
                    "last_traded_time": 1726732800,
                    "exch_feed_time": 1726732801,
                    "type": "sf",
                    "symbol": "NSE:NIFTY26SEP24500CE",
                },
                indent=2,
            )
        )
        lines.append("```")
        lines.append("")
        lines.append("### data_ws DepthUpdate (5-level)")
        lines.append("")
        lines.append("```json")
        lines.append(
            json.dumps(
                {
                    "bid_price1": 120.0,
                    "bid_size1": 1500,
                    "ask_price1": 120.5,
                    "ask_size1": 1200,
                    "type": "dp",
                    "symbol": "NSE:NIFTY26SEP24500CE",
                },
                indent=2,
            )
        )
        lines.append("```")
        lines.append("")
        lines.append("### tbt_ws Depth (50-level protobuf decoded)")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(serialize_depth(_example_depth()), indent=2))
        lines.append("```")
        lines.append("")
    lines.extend(["## Data-quality findings", ""])
    for summary in summaries:
        if summary.message_count == 0:
            lines.append(f"- `{summary.symbol}` / {summary.feed}:{summary.data_type}: no messages")
        if summary.sequence_gaps:
            lines.append(
                f"- `{summary.symbol}`: {summary.sequence_gaps} sequence gap(s) observed"
            )
        if summary.duplicate_count:
            lines.append(
                f"- `{summary.symbol}`: {summary.duplicate_count} duplicate payload(s)"
            )
        if summary.median_latency_ms is not None:
            lines.append(
                f"- `{summary.symbol}`: median local latency "
                f"{summary.median_latency_ms:.1f} ms"
            )
        for note in summary.notes:
            lines.append(f"- `{summary.symbol}`: {note}")
    for feed_key, errors in data_errors.items():
        for error in errors:
            lines.append(f"- data_ws/{feed_key} error: {error}")
    for error in tbt_errors:
        lines.append(f"- tbt_ws error: {error}")
    lines.extend(
        [
            "",
            "## Recommended Layer 1 contract changes",
            "",
            "- Add `DepthBookEvent` with explicit `bid_levels`/`ask_levels` arrays, "
            "`total_buy_qty`/`total_sell_qty`, `provider_sequence`, `is_snapshot`, and "
            "separate `source_time` from `receive_time`.",
            "- Extend `TICK` normalization to retain `exch_feed_time`, `last_traded_time`, "
            "`last_traded_qty`, `bid_size`, `ask_size`, and `type` (`if`/`sf`/`dp`).",
            "- Add `TBT_DEPTH` canonical event for 50-level protobuf depth with sequence "
            "numbers and snapshot/delta flag.",
            "- Do not infer `aggressor_side`; keep it absent unless a dedicated trade tape "
            "event type is subscribed.",
            "",
            "## Positional strategy sufficiency",
            "",
            _positional_verdict(summaries),
            "",
            "## CAS sufficiency",
            "",
            _cas_verdict(summaries, entitlement),
            "",
            "## Gaps requiring another provider",
            "",
            _provider_gaps(symbols, summaries, entitlement),
            "",
            "## Documented limits (verify before production)",
            "",
            f"- Data socket symbol limit: {DATA_WS_SYMBOL_LIMIT}",
            f"- TBT symbols per connection: {TBT_SYMBOL_LIMIT_PER_CONNECTION}",
            f"- TBT channels: {TBT_CHANNEL_RANGE[0]}-{TBT_CHANNEL_RANGE[1]}",
            f"- TBT connections per app/user: {TBT_CONNECTIONS_PER_APP}",
            "- TBT is a premium entitlement; REST depth remains 5 levels.",
            "- TBT documents NSE/NFO only; MCX requires the data socket or another vendor.",
            "",
        ]
    )
    return "\n".join(lines)


def _example_depth() -> Any:
    return type(
        "_Depth",
        (),
        {
            "tbq": 50000,
            "tsq": 48000,
            "bidprice": [120.0] + [0.0] * 49,
            "askprice": [120.5] + [0.0] * 49,
            "bidqty": [1500] + [0] * 49,
            "askqty": [1200] + [0] * 49,
            "bidordn": [12] + [0] * 49,
            "askordn": [10] + [0] * 49,
            "snapshot": True,
            "timestamp": 1_700_000_000,
            "sendtime": 1_700_000_001,
            "seqNo": 42,
        },
    )()


def pick_liquid_option(chain_capture: Any) -> str | None:
    """Pick the most liquid option symbol from one chain response."""
    return _pick_liquid_option(chain_capture)


def _pick_liquid_option(chain_capture: Any) -> str | None:
    data = chain_capture.payload.get("data", chain_capture.payload)
    rows = data.get("optionsChain") or data.get("options_chain") or []
    if not isinstance(rows, list):
        return None
    underlying_ltp = None
    candidates: list[tuple[int, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("option_type") not in {"CE", "PE"}:
            if row.get("strike_price") in {-1, None}:
                underlying_ltp = row.get("ltp") or row.get("fp")
            continue
        symbol = row.get("symbol")
        if not isinstance(symbol, str):
            continue
        volume = int(row.get("volume") or 0)
        oi = int(row.get("oi") or 0)
        strike = row.get("strike_price")
        if underlying_ltp is not None and isinstance(strike, (int, float)):
            distance = abs(float(strike) - float(underlying_ltp))
        else:
            distance = 0.0
        candidates.append((volume + oi, -distance, symbol))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][2]


def _resolve_mcx_symbol(feed: FyersMarketFeed, prefix: str) -> str:
    for candidate in (prefix, f"{prefix}25SEPFUT", f"{prefix}25OCTFUT"):
        try:
            capture = feed.fetch_quotes([candidate])
        except Exception:
            continue
        rows = capture.payload.get("d", [])
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict) and row.get("s") == "ok":
                    symbol = row.get("n")
                    if isinstance(symbol, str):
                        return symbol
    return prefix


def _classify_data_ws_message(message: Mapping[str, Any]) -> str:
    msg_type = message.get("type")
    if msg_type in {"cn", "sub", "if", "sf", "dp"}:
        return str(msg_type)
    if message.get("bid_price1") or message.get("ask_price1"):
        return "depth"
    if message.get("ltp") is not None:
        return "quote"
    return "control"


def _union_keys(payloads: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    keys: set[str] = set()
    for payload in payloads:
        keys.update(str(key) for key in payload)
    return tuple(sorted(keys))


def _sequence_stats(samples: Sequence[CapturedMessage]) -> tuple[int, int]:
    sequences = [
        int(sample.payload["sequence_no"])
        for sample in samples
        if isinstance(sample.payload.get("sequence_no"), int)
    ]
    gaps = 0
    for prev, curr in zip(sequences, sequences[1:], strict=False):
        if curr > prev + 1:
            gaps += curr - prev - 1
    fingerprints = [json.dumps(sample.payload, sort_keys=True, default=str) for sample in samples]
    duplicate_count = len(fingerprints) - len(set(fingerprints))
    return gaps, duplicate_count


def _latency_ms(samples: Sequence[CapturedMessage]) -> list[float]:
    latencies: list[float] = []
    for sample in samples:
        exchange_ts = sample.payload.get("exchange_timestamp")
        if exchange_ts is None:
            exchange_ts = sample.payload.get("exch_feed_time")
        if exchange_ts is None:
            exchange_ts = sample.payload.get("send_timestamp")
        if not isinstance(exchange_ts, (int, float)):
            continue
        # FYERS timestamps are epoch seconds or milliseconds; infer unit by magnitude.
        if exchange_ts > 1_000_000_000_000:
            source = datetime.fromtimestamp(exchange_ts / 1000.0, tz=UTC)
        elif exchange_ts > 1_000_000_000:
            source = datetime.fromtimestamp(exchange_ts, tz=UTC)
        else:
            continue
        latencies.append(max((sample.receive_time - source).total_seconds() * 1000.0, 0.0))
    return latencies


def _inter_arrival_ms(samples: Sequence[CapturedMessage]) -> list[float]:
    if len(samples) < 2:
        return []
    ordered = sorted(samples, key=lambda sample: sample.receive_time)
    intervals: list[float] = []
    for prev, curr in zip(ordered, ordered[1:], strict=False):
        delta = (curr.receive_time - prev.receive_time).total_seconds() * 1000.0
        intervals.append(delta)
    return intervals


def _all_summaries(
    symbols: ProbeSymbolSet,
    data_symbol_updates: Sequence[CapturedMessage],
    data_depth_updates: Sequence[CapturedMessage],
    tbt_updates: Sequence[CapturedMessage],
) -> list[ProbeSummary]:
    summaries: list[ProbeSummary] = []
    for symbol in symbols.all_symbols():
        summaries.append(
            analyze_samples(
                data_symbol_updates,
                feed="data_ws",
                data_type="SymbolUpdate",
                symbol=symbol,
            )
        )
        if symbol.endswith("-INDEX"):
            continue
        summaries.append(
            analyze_samples(
                data_depth_updates,
                feed="data_ws",
                data_type="DepthUpdate",
                symbol=symbol,
            )
        )
    for symbol in symbols.tbt_eligible():
        summaries.append(
            analyze_samples(
                tbt_updates,
                feed="tbt_ws",
                data_type="Depth",
                symbol=symbol,
            )
        )
    return summaries


def _example_payloads(
    data_symbol_updates: Sequence[CapturedMessage],
    data_depth_updates: Sequence[CapturedMessage],
    tbt_updates: Sequence[CapturedMessage],
) -> list[tuple[str, dict[str, Any]]]:
    examples: list[tuple[str, dict[str, Any]]] = []
    for label, samples in (
        ("data_ws SymbolUpdate", data_symbol_updates),
        ("data_ws DepthUpdate", data_depth_updates),
        ("tbt_ws Depth", tbt_updates),
    ):
        if samples:
            examples.append((label, samples[0].payload))
    return examples


def _positional_verdict(summaries: Sequence[ProbeSummary]) -> str:
    quote_ok = any(
        summary.feed == "data_ws"
        and summary.data_type == "SymbolUpdate"
        and summary.message_count > 0
        and summary.has_ltp
        for summary in summaries
    )
    depth_ok = any(
        summary.message_count > 0 and (summary.bid_levels or 0) >= 1
        for summary in summaries
        if summary.data_type in {"DepthUpdate", "Depth"}
    )
    if quote_ok and depth_ok:
        return (
            "Sufficient for conservative positional protection and sizing that rely on "
            "top-of-book / 5-level depth plus REST chain/OI. Not sufficient for "
            "sub-second microstructure without TBT entitlement."
        )
    return (
        "Insufficient live evidence in this bounded window; positional entry should "
        "remain blocked until SymbolUpdate and depth paths produce VALID samples."
    )


def _cas_verdict(
    summaries: Sequence[ProbeSummary],
    entitlement: Mapping[str, Any],
) -> str:
    depth_summaries = [
        summary
        for summary in summaries
        if summary.data_type in {"DepthUpdate", "Depth"} and summary.message_count > 0
    ]
    max_levels = max(
        (summary.bid_levels or 0, summary.ask_levels or 0)
        for summary in depth_summaries
    ) if depth_summaries else (0, 0)
    has_two_updates = any(summary.message_count >= 2 for summary in depth_summaries)
    has_flow = any(summary.has_last_traded_qty for summary in summaries)
    if not entitlement.get("entitled"):
        return (
            "Not sufficient for CAS as implemented: TBT entitlement missing and data "
            "socket DepthUpdate is 5-level without confirmed trade-tape aggressor side."
        )
    if max(max_levels) < 10 or not has_two_updates:
        return (
            "TBT entitlement present but bounded sample did not yield enough depth "
            "history for `cas-microstructure-v1`; keep CAS disabled."
        )
    if not has_flow:
        return (
            "Depth history may support auction/microprice features, but "
            "`cas_trade_flow_imbalance` still lacks authoritative trade/aggressor "
            "fields; CAS remains insufficient."
        )
    return "Potentially sufficient after a dedicated promotion gate; not enabled by this probe."


def _provider_gaps(
    symbols: ProbeSymbolSet,
    summaries: Sequence[ProbeSummary],
    entitlement: Mapping[str, Any],
) -> str:
    gaps: list[str] = []
    if not entitlement.get("entitled"):
        gaps.append("50-level TBT depth and sequence-aware book replay (premium entitlement).")
    mcx = [symbols.mcx_gold, symbols.mcx_crude]
    mcx_msgs = sum(
        1
        for summary in summaries
        if summary.symbol in mcx and summary.message_count > 0
    )
    if mcx_msgs == 0:
        gaps.append("Verified MCX continuous-symbol depth via a vendor with MCX TBT or colocated feed.")
    if not any(summary.has_aggressor_side for summary in summaries):
        gaps.append(
            "Exchange-grade trade tape with explicit aggressor side for CAS order-flow features."
        )
    gaps.append("Historical L2/L3 order book (live feeds are not historical archives).")
    return "\n".join(f"- {gap}" for gap in gaps)
