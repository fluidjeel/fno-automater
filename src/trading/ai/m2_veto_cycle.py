"""Build one five-minute M2 veto from quotes and 5-minute bars."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from trading.ai.llm_settings import load_llm_settings
from trading.ai.openai_compat import OpenAICompatLlm
from trading.data.fyers.auth import ensure_access_token
from trading.data.fyers.client import FyersMarketFeed
from trading.data.settings import FyersSettings
from trading.domain.clock import Clock, WallClock
from trading.trade.m2_veto import (
    M2VetoAction,
    M2VetoConfig,
    M2VetoVerdict,
    bar_move_state,
    in_session,
    load_m2_veto_config,
    parse_model_verdict,
    write_verdict,
)

__all__ = ["run_m2_veto_cycle"]

_MIN_COMPLETED_BARS = 3
_CANDLE_WIDTH = 5

_PROMPT = (
    "You are a veto on a Nifty directional option entry. You do not place orders. "
    "Use only the facts in the user message. "
    "VETO when the last 5-minute move looks exhausted, volatility is quiet, "
    "or the latest quote has already reversed the bar. "
    "SIZE_DOWN when the 5-minute direction remains but the latest quote is stalling. "
    "APPROVE only when that direction is still developing on the latest quote. "
    "Reply with JSON only: "
    '{"action": "APPROVE" | "VETO" | "SIZE_DOWN", "reasons": ["one short sentence"]}'
)


def run_m2_veto_cycle(
    *,
    repo_root: Path | None = None,
    clock: Clock | None = None,
    config: M2VetoConfig | None = None,
) -> M2VetoVerdict | None:
    """Write one verdict during the session. Outside the session, write nothing."""
    root = repo_root if repo_root is not None else Path(__file__).resolve().parents[3]
    now_clock = clock or WallClock()
    now = now_clock.now_utc()
    loaded = config or load_m2_veto_config()
    if not loaded.enabled or not in_session(now, loaded):
        return None
    print(f"broker login: {ensure_access_token(root)}", flush=True)
    try:
        facts = _facts(root, loaded, now)
    except Exception as exc:
        verdict = _code_veto(now, loaded, f"market context unavailable: {exc}")
        write_verdict(verdict, loaded)
        return verdict
    if facts.get("quote_stale"):
        verdict = _code_veto(now, loaded, "latest Nifty quote is older than one minute")
        write_verdict(verdict, loaded)
        return verdict
    try:
        settings = load_llm_settings(root)
        print(
            f"m2 veto request model: {loaded.model_id} thinking: {loaded.thinking}",
            flush=True,
        )
        text = OpenAICompatLlm(settings).complete_text(
            [
                {"role": "system", "content": _PROMPT},
                {"role": "user", "content": json.dumps(facts)},
            ],
            json_object=True,
            model=loaded.model_id,
            thinking=loaded.thinking,
        )
        payload = json.loads(text)
        if not isinstance(payload, dict):
            raise ValueError("model reply was not an object")
        verdict = parse_model_verdict(payload, now=now, config=loaded)
    except Exception as exc:
        verdict = _code_veto(now, loaded, f"veto model unavailable: {exc}")
    write_verdict(verdict, loaded)
    return verdict


def _code_veto(now: datetime, config: M2VetoConfig, reason: str) -> M2VetoVerdict:
    return parse_model_verdict(
        {"action": M2VetoAction.VETO.value, "reasons": [reason[:200]]},
        now=now,
        config=config,
    )


def _facts(root: Path, config: M2VetoConfig, now: datetime) -> dict[str, Any]:
    settings = FyersSettings.from_repo_root_with_cache(root)
    feed = FyersMarketFeed(settings, WallClock())
    quotes = feed.fetch_quotes((config.nifty_symbol, config.vix_symbol))
    nifty = _quote_ltp(quotes.payload, config.nifty_symbol)
    vix = _quote_ltp(quotes.payload, config.vix_symbol)
    day = now.astimezone(ZoneInfo("Asia/Kolkata")).date()
    start = (day - timedelta(days=2)).isoformat()
    history = feed.fetch_history(
        config.nifty_symbol,
        resolution=config.bar_resolution,
        range_from=start,
        range_to=day.isoformat(),
    )
    candles = _candles(history.payload)
    completed = _completed(candles, now)
    if nifty is None or vix is None or len(completed) < _MIN_COMPLETED_BARS:
        raise ValueError("need Nifty, India VIX, and three completed 5-minute bars")
    last = completed[-1]
    quote_age = int((now - quotes.received_at).total_seconds())
    return {
        "as_of": now.isoformat(),
        "nifty_quote": str(nifty),
        "india_vix": str(vix),
        "quote_age_seconds": quote_age,
        "quote_stale": quote_age > config.quote_max_age_seconds,
        "last_completed_5m": {
            "open": str(last[1]),
            "high": str(last[2]),
            "low": str(last[3]),
            "close": str(last[4]),
        },
        "move_versus_last_5m_bar": bar_move_state(
            quote=nifty,
            bar_open=last[1],
            bar_high=last[2],
            bar_low=last[3],
            bar_close=last[4],
        ),
        "prior_5m_closes": [str(row[4]) for row in completed[-config.completed_bars :]],
        "news": "no fresh news feed; the local news file is not used",
    }


def _quote_ltp(payload: dict[str, Any], symbol: str) -> Decimal | None:
    rows = payload.get("d")
    if not isinstance(rows, list):
        return None
    for row in rows:
        if not isinstance(row, dict) or row.get("n") != symbol:
            continue
        value = row.get("v")
        if not isinstance(value, dict):
            return None
        raw = value.get("lp")
        if raw is None:
            return None
        return Decimal(str(raw))
    return None


def _candles(payload: dict[str, Any]) -> list[list[Decimal]]:
    raw = payload.get("candles")
    if not isinstance(raw, list):
        return []
    rows: list[list[Decimal]] = []
    for row in raw:
        if not isinstance(row, list) or len(row) < _CANDLE_WIDTH:
            continue
        rows.append([Decimal(str(item)) for item in row[:_CANDLE_WIDTH]])
    return rows


def _completed(candles: list[list[Decimal]], now: datetime) -> list[list[Decimal]]:
    """Drop the bar that is still forming. Candle time is unix seconds."""
    cutoff = Decimal(int(now.timestamp()) - 300)
    done = [row for row in candles if row[0] <= cutoff]
    return done or candles[:-1]
