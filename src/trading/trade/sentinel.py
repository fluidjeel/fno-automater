"""Real-time event-driven stop-loss sentinel.

Monitors streaming price updates for open positions and fires immediate exit
evaluations on stop/target breaches without waiting for periodic polling intervals.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from trading.domain.clock import WallClock
from trading.domain.contracts.intent import TradeIntent
from trading.domain.contracts.position import ExitPolicy, PositionState
from trading.domain.enums import ExitScope, ReasonCode, Side, TradeState
from trading.domain.primitives import Price
from trading.trade.exits import ExitEngine, ExitEvaluation, ExitKind, monitor_leg

__all__ = [
    "ExitCallback",
    "MonitoredTrade",
    "SentinelEvent",
    "StopSentinel",
]

logger = logging.getLogger(__name__)

ExitCallback = Callable[[str, ExitEvaluation, Price, datetime], Any]


@dataclass(frozen=True, slots=True)
class MonitoredTrade:
    """Internal tracking record for an open position being monitored."""

    trade_id: str
    symbol: str
    side: Side
    exit_policy: ExitPolicy
    intent: TradeIntent
    position: PositionState


@dataclass(frozen=True, slots=True)
class SentinelEvent:
    """Event emitted when the sentinel detects an exit condition."""

    trade_id: str
    symbol: str
    trigger_price: Price
    evaluation: ExitEvaluation
    timestamp: datetime


class StopSentinel:
    """Event-driven price watcher triggering sub-second exit evaluations."""

    def __init__(
        self,
        *,
        on_exit: ExitCallback | None = None,
        exit_engine: ExitEngine | None = None,
    ) -> None:
        self._on_exit = on_exit
        self._exit_engine = exit_engine or ExitEngine()
        self._lock = threading.Lock()
        self._monitored: dict[str, MonitoredTrade] = {}
        self._symbol_to_trade_ids: dict[str, set[str]] = {}
        self._events: list[SentinelEvent] = []

    @property
    def monitored_trade_count(self) -> int:
        """Total number of trades currently monitored."""
        with self._lock:
            return len(self._monitored)

    @property
    def monitored_symbols(self) -> tuple[str, ...]:
        """Unique symbols subscribed to for monitoring."""
        with self._lock:
            return tuple(self._symbol_to_trade_ids.keys())

    @property
    def triggered_events(self) -> tuple[SentinelEvent, ...]:
        """History of exit events fired by the sentinel."""
        with self._lock:
            return tuple(self._events)

    def register_position(
        self,
        position: PositionState,
        intent: TradeIntent,
    ) -> bool:
        """Register an open position for sub-second price monitoring.

        Returns True if the position was successfully registered.
        """
        if position.state is not TradeState.OPEN:
            return False

        leg = monitor_leg(intent)
        symbol = getattr(leg.contract, "trading_symbol", None) or leg.contract.symbol
        side = leg.side

        monitored = MonitoredTrade(
            trade_id=position.trade_id,
            symbol=symbol,
            side=side,
            exit_policy=position.exit_policy,
            intent=intent,
            position=position,
        )

        with self._lock:
            self._monitored[position.trade_id] = monitored
            if symbol not in self._symbol_to_trade_ids:
                self._symbol_to_trade_ids[symbol] = set()
            self._symbol_to_trade_ids[symbol].add(position.trade_id)

        logger.info(
            "Sentinel registered trade %s on %s (stop=%s, target=%s)",
            position.trade_id,
            symbol,
            position.exit_policy.stop_price,
            position.exit_policy.target_price,
        )
        return True

    def unregister_position(self, trade_id: str) -> None:
        """Remove a position from active monitoring (e.g. after fill/close)."""
        with self._lock:
            monitored = self._monitored.pop(trade_id, None)
            if monitored is not None:
                symbol = monitored.symbol
                trade_ids = self._symbol_to_trade_ids.get(symbol)
                if trade_ids:
                    trade_ids.discard(trade_id)
                    if not trade_ids:
                        self._symbol_to_trade_ids.pop(symbol, None)

    def on_tick(
        self,
        symbol: str,
        *,
        ltp: Price,
        bid: Price | None = None,
        ask: Price | None = None,
        timestamp: datetime | None = None,
    ) -> tuple[SentinelEvent, ...]:
        """Process an incoming tick for a symbol and fire any breached stops."""
        ts = timestamp or WallClock().now_utc()
        candidates: list[MonitoredTrade] = []

        with self._lock:
            trade_ids = self._symbol_to_trade_ids.get(symbol, set())
            for tid in trade_ids:
                item = self._monitored.get(tid)
                if item is not None:
                    candidates.append(item)

        if not candidates:
            return ()

        events: list[SentinelEvent] = []
        for monitored in candidates:
            event = self._check_breach(monitored, ltp=ltp, bid=bid, ask=ask, now=ts)
            if event is not None:
                with self._lock:
                    self._events.append(event)
                    # Automatically unregister once triggered to prevent duplicate fires
                    self._monitored.pop(monitored.trade_id, None)
                    symbol_set = self._symbol_to_trade_ids.get(symbol)
                    if symbol_set:
                        symbol_set.discard(monitored.trade_id)
                        if not symbol_set:
                            self._symbol_to_trade_ids.pop(symbol, None)

                events.append(event)
                if self._on_exit is not None:
                    try:
                        self._on_exit(
                            event.trade_id,
                            event.evaluation,
                            event.trigger_price,
                            event.timestamp,
                        )
                    except Exception:
                        logger.exception(
                            "Exit callback failed for trade %s in sentinel",
                            event.trade_id,
                        )

        return tuple(events)

    def _check_breach(
        self,
        monitored: MonitoredTrade,
        *,
        ltp: Price,
        bid: Price | None,
        ask: Price | None,
        now: datetime,
    ) -> SentinelEvent | None:
        """Check if price breached stop or target for a monitored trade."""
        policy = monitored.exit_policy
        if policy.scope is not ExitScope.LEG_PRICE:
            return None

        # Determine authoritative monitor price based on side.
        # Long position exits to BID (fallback LTP).
        # Short position exits to ASK (fallback LTP).
        if monitored.side is Side.BUY:
            monitor_price = bid if bid is not None else ltp
        else:
            monitor_price = ask if ask is not None else ltp

        kind = ExitKind.NONE
        detail = ""

        # 1. Check time exit
        if policy.time_exit is not None and now >= policy.time_exit:
            kind = ExitKind.TIME
            detail = "sentinel: scheduled time exit reached"

        # 2. Check stop price breach
        elif policy.stop_price is not None and (
            (monitored.side is Side.BUY and monitor_price <= policy.stop_price)
            or (monitored.side is Side.SELL and monitor_price >= policy.stop_price)
        ):
            kind = ExitKind.STOP
            detail = (
                f"sentinel: stop breached (price {monitor_price} vs "
                f"{policy.stop_price})"
            )

        # 3. Check target price reach
        elif policy.target_price is not None and (
            (monitored.side is Side.BUY and monitor_price >= policy.target_price)
            or (monitored.side is Side.SELL and monitor_price <= policy.target_price)
        ):
            kind = ExitKind.TARGET
            detail = (
                f"sentinel: target reached (price {monitor_price} vs "
                f"{policy.target_price})"
            )

        if kind is not ExitKind.NONE:
            evaluation = ExitEvaluation(
                kind=kind,
                reason_code=ReasonCode.OK,
                detail=detail,
                updated_policy=policy,
            )
            return SentinelEvent(
                trade_id=monitored.trade_id,
                symbol=monitored.symbol,
                trigger_price=monitor_price,
                evaluation=evaluation,
                timestamp=now,
            )

        return None
