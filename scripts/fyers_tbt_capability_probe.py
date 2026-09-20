#!/usr/bin/env python3
"""Bounded read-only FYERS data/TBT WebSocket capability probe."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from trading.data.fyers.capability_probe import (
    ProbeSymbolSet,
    analyze_samples,
    check_tbt_entitlement,
    collect_data_ws,
    collect_tbt_ws,
    generate_markdown_report,
    resolve_probe_symbols,
)
from trading.data.fyers.client import FyersMarketFeed
from trading.data.settings import FyersSettings
from trading.domain.clock import WallClock


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _load_settings(root: Path) -> FyersSettings:
    settings = FyersSettings.from_repo_root(root)
    cached = settings.load_cached_token(root)
    if cached and not settings.fyers_access_token:
        settings = settings.model_copy(update={"fyers_access_token": cached})
    return settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--duration-seconds",
        type=float,
        default=20.0,
        help="Per-feed capture window (default: 20)",
    )
    parser.add_argument(
        "--max-messages",
        type=int,
        default=50,
        help="Maximum messages per feed (default: 50)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Artifact directory (default: tests/artifacts/fyers_tbt_probe)",
    )
    args = parser.parse_args(argv)

    root = _repo_root()
    settings = _load_settings(root)
    feed = FyersMarketFeed(settings, WallClock())
    fallback = ProbeSymbolSet(
        nifty_option="NSE:NIFTY26SEP24500CE",
        stock_option="NSE:RELIANCE26SEP1400CE",
        mcx_gold="MCX:GOLDM25OCTFUT",
        mcx_crude="MCX:CRUDEOILM25OCTFUT",
    )
    symbols = resolve_probe_symbols(feed, fallback=fallback)
    rest_errors: list[str] = []
    if symbols.nifty_option == fallback.nifty_option:
        rest_errors.append("REST option-chain lookup unavailable; using fallback NIFTY option")
    try:
        entitlement = check_tbt_entitlement(settings)
    except ValueError as exc:
        entitlement = {
            "http_status": None,
            "entitled": False,
            "socket_url": None,
            "message": str(exc),
            "body": {},
        }
        rest_errors.append(str(exc))

    print("Resolved symbols:")
    for label, value in (
        ("nifty_index", symbols.nifty_index),
        ("nifty_option", symbols.nifty_option),
        ("stock_option", symbols.stock_option),
        ("mcx_gold", symbols.mcx_gold),
        ("mcx_crude", symbols.mcx_crude),
    ):
        print(f"  {label}: {value}")

    print(f"TBT entitlement: {entitlement.get('entitled')} ({entitlement.get('http_status')})")

    all_symbols = symbols.all_symbols()
    try:
        data_symbol_updates, symbol_errors = collect_data_ws(
            settings,
            all_symbols,
            data_type="SymbolUpdate",
            duration_seconds=args.duration_seconds,
            max_messages=args.max_messages,
        )
    except Exception as exc:
        data_symbol_updates = ()
        symbol_errors = (f"SymbolUpdate capture failed: {exc}",)
    depth_symbols = [symbol for symbol in all_symbols if not symbol.endswith("-INDEX")]
    data_depth_updates: tuple = ()
    depth_errors: tuple[str, ...] = ()
    if depth_symbols:
        try:
            data_depth_updates, depth_errors = collect_data_ws(
                settings,
                depth_symbols,
                data_type="DepthUpdate",
                duration_seconds=args.duration_seconds,
                max_messages=args.max_messages,
            )
        except Exception as exc:
            data_depth_updates = ()
            depth_errors = (f"DepthUpdate capture failed: {exc}",)

    tbt_updates: tuple = ()
    tbt_errors: tuple[str, ...] = ()
    tbt_symbols = symbols.tbt_eligible()
    if entitlement.get("entitled") and tbt_symbols:
        try:
            tbt_updates, tbt_errors = collect_tbt_ws(
                settings,
                tbt_symbols,
                duration_seconds=args.duration_seconds,
                max_messages=args.max_messages,
            )
        except Exception as exc:
            tbt_updates = ()
            tbt_errors = (f"TBT capture failed: {exc}",)
    elif not entitlement.get("entitled"):
        tbt_errors = (
            "TBT entitlement endpoint did not return success; skipping live TBT capture.",
        )
    if rest_errors:
        symbol_errors = tuple(rest_errors) + symbol_errors

    output_dir = args.output_dir or root / "tests" / "artifacts" / "fyers_tbt_probe"
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")

    def _dump(name: str, messages: tuple) -> None:
        path = output_dir / f"{stamp}_{name}.jsonl"
        with path.open("w", encoding="utf-8") as handle:
            for message in messages:
                row = {
                    "feed": message.feed,
                    "data_type": message.data_type,
                    "symbol": message.symbol,
                    "receive_time": message.receive_time.isoformat(),
                    "message_kind": message.message_kind,
                    "payload": message.payload,
                }
                handle.write(json.dumps(row, default=str) + "\n")
        print(f"Wrote {path} ({len(messages)} messages)")

    _dump("data_symbol_update", data_symbol_updates)
    _dump("data_depth_update", data_depth_updates)
    _dump("tbt_depth", tbt_updates)

    entitlement_path = output_dir / f"{stamp}_entitlement.json"
    entitlement_path.write_text(
        json.dumps(entitlement, indent=2, default=str),
        encoding="utf-8",
    )

    report = generate_markdown_report(
        symbols=symbols,
        entitlement=entitlement,
        data_symbol_updates=data_symbol_updates,
        data_depth_updates=data_depth_updates,
        tbt_updates=tbt_updates,
        data_errors={
            "SymbolUpdate": symbol_errors,
            "DepthUpdate": depth_errors,
        },
        tbt_errors=tbt_errors,
        generated_at=datetime.now(tz=UTC),
    )
    report_path = output_dir / f"{stamp}_REPORT.md"
    report_path.write_text(report, encoding="utf-8")
    print(f"Wrote {report_path}")

    summaries = [
        analyze_samples(data_symbol_updates, feed="data_ws", data_type="SymbolUpdate", symbol=symbol)
        for symbol in all_symbols
    ]
    print("\nQuick summary:")
    for summary in summaries:
        print(
            f"  {summary.symbol} SymbolUpdate: {summary.message_count} msgs, "
            f"fields={len(summary.field_keys)}, ltp={summary.has_ltp}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
