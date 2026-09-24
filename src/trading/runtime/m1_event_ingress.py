"""Production ingress that turns provider quotes into M1 session events.

The 60-second poll must not invent M1 entries. Qualifying quote updates call
``PaperSession.submit_m1_event`` directly.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from trading.domain.contracts.snapshot import MarketQuote
from trading.runtime.cas_event_path import (
    ORACLE_MEASURED,
    M1ProviderEvent,
    active_m1_window,
)
from trading.runtime.paper_session import PaperSession

__all__ = [
    "M1EventIngress",
    "build_m1_provider_event",
]


def build_m1_provider_event(
    *,
    receive_time: datetime,
    quote_time: datetime | None,
    decided_at: datetime,
    provenance: str = ORACLE_MEASURED,
    profile_observed: str = "quote_only",
    depth_fields_present: bool = False,
    episode_id: str = "",
    disconnected: bool = False,
    exchange_timestamp_observed: bool = False,
) -> M1ProviderEvent:
    """Build one provider event from observed quote timestamps."""
    return M1ProviderEvent(
        receive_time=receive_time,
        decided_at=decided_at,
        provenance=provenance,
        profile_observed=profile_observed,
        depth_fields_present=depth_fields_present,
        event_time=quote_time,
        quote_time=quote_time,
        episode_id=episode_id,
        disconnected=disconnected,
        allow_simulated_fixture=False,
        exchange_timestamp_observed=exchange_timestamp_observed
        or quote_time is not None,
    )


@dataclass
class M1EventIngress:
    """Subscribe-style callback that submits M1 events into a live session."""

    session: PaperSession
    now_utc: Callable[[], datetime]
    min_interval_ms: int = 50
    _last_submit_at: datetime | None = None

    def on_quote(
        self,
        symbol: str,
        quote: MarketQuote,
        *,
        receive_time: datetime | None = None,
        disconnected: bool = False,
    ) -> object | None:
        """Submit one M1 event when the session is inside an M1 scan window."""
        _ = symbol
        now = self.now_utc()
        cfg = self.session.session_config.cas_event_driven
        if not cfg.enabled:
            return None
        if active_m1_window(now, cfg) is None:
            return None
        if self._last_submit_at is not None:
            gap_ms = int((now - self._last_submit_at).total_seconds() * 1000)
            if gap_ms < self.min_interval_ms:
                return None
        receive = receive_time or now
        quote_time = getattr(quote, "exchange_ts", None) or getattr(
            quote, "as_of", None
        )
        if not isinstance(quote_time, datetime):
            quote_time = None
        event = build_m1_provider_event(
            receive_time=receive,
            quote_time=quote_time,
            decided_at=now,
            profile_observed=cfg.profile,
            depth_fields_present=bool(
                getattr(quote, "bid_size", None) and getattr(quote, "ask_size", None)
            ),
            disconnected=disconnected,
            exchange_timestamp_observed=quote_time is not None,
        )
        self._last_submit_at = now
        return self.session.submit_m1_event(event)
