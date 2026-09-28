"""Missed-move ledger: large moves per mode horizon and why each mode sat out.

A move is "large" when a horizon-length window's open-to-close change reaches
``threshold_atr`` times the average true range of the preceding windows, so
the yardstick is point-in-time and scales with each mode's horizon.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal

from trading.domain.contracts.discovery_decision import DiscoveryDecision
from trading.domain.contracts.forecast import ModeForecast
from trading.domain.enums import DiscoveryDecisionKind, ModeId, ReasonCode
from trading.forecast.bars import Bar

__all__ = [
    "LargeMove",
    "MissedMove",
    "detect_large_moves",
    "format_missed_moves",
    "missed_moves",
]

_ZERO = Decimal(0)
_HALF = Decimal("0.5")
_QUANT = Decimal("0.0001")
# A gap of more than a few bars between consecutive bars is a session break.
_SESSION_BREAK_BARS = 6


def _q(value: Decimal) -> Decimal:
    return value.quantize(_QUANT, rounding=ROUND_HALF_EVEN)


@dataclass(frozen=True, slots=True)
class LargeMove:
    """One horizon window whose move cleared the ATR multiple."""

    start: datetime
    end: datetime
    direction: int
    move_fraction: Decimal
    atr_multiple: Decimal


@dataclass(frozen=True, slots=True)
class MissedMove:
    """A large move and what the mode did about it at the start of the window."""

    mode_id: ModeId
    move: LargeMove
    forecasts: int
    best_aligned_p: Decimal | None
    forecast_direction_agreed: bool
    traded: bool
    blocking_reasons: tuple[tuple[ReasonCode, int], ...]

    @property
    def verdict(self) -> str:
        if self.traded:
            return "CAUGHT"
        if self.forecasts == 0:
            return "NOT_FORECAST"
        if not self.forecast_direction_agreed:
            return "WRONG_VIEW"
        return "BLOCKED"


def _windows(
    bars: Sequence[Bar], size: int, *, bar_seconds: int, intraday: bool
) -> list[Sequence[Bar]]:
    """Consecutive windows; intraday horizons never span the overnight gap."""
    if not intraday:
        return [bars[i : i + size] for i in range(0, len(bars) - size + 1, size)]
    sessions: list[list[Bar]] = []
    for bar in bars:
        gap = (
            None
            if not sessions
            else (bar.start - sessions[-1][-1].start).total_seconds()
        )
        if gap is None or gap > _SESSION_BREAK_BARS * bar_seconds:
            sessions.append([bar])
        else:
            sessions[-1].append(bar)
    windows: list[Sequence[Bar]] = []
    for session in sessions:
        windows.extend(
            session[i : i + size] for i in range(0, len(session) - size + 1, size)
        )
    return windows


def detect_large_moves(
    bars: Sequence[Bar],
    *,
    horizon_bars: int,
    bar_seconds: int,
    threshold_atr: Decimal,
    atr_windows: int,
    session_bars: int,
) -> tuple[LargeMove, ...]:
    """Non-overlapping horizon windows whose move is >= ``threshold_atr`` x ATR."""
    ordered = sorted(bars, key=lambda bar: bar.start)
    windows = _windows(
        ordered,
        max(1, horizon_bars),
        bar_seconds=bar_seconds,
        intraday=horizon_bars <= session_bars,
    )
    ranges: list[Decimal] = []
    moves: list[LargeMove] = []
    previous_close: Decimal | None = None
    for window in windows:
        high = max(bar.high for bar in window)
        low = min(bar.low for bar in window)
        true_range = high - low
        if previous_close is not None:
            true_range = max(
                true_range, abs(high - previous_close), abs(low - previous_close)
            )
        history = ranges[-atr_windows:]
        if len(history) >= atr_windows:
            atr = sum(history, _ZERO) / len(history)
            change = window[-1].close - window[0].open
            if atr > 0 and abs(change) >= threshold_atr * atr:
                moves.append(
                    LargeMove(
                        start=window[0].start,
                        end=window[-1].start + timedelta(seconds=bar_seconds),
                        direction=1 if change > 0 else -1,
                        move_fraction=_q(change / window[0].open),
                        atr_multiple=_q(abs(change) / atr),
                    )
                )
        ranges.append(true_range)
        previous_close = window[-1].close
    return tuple(moves)


def _entry_window(move: LargeMove) -> tuple[datetime, datetime]:
    """Decisions made in the first half of the move could still have caught it."""
    return move.start, move.start + (move.end - move.start) / 2


def missed_moves(
    mode_id: ModeId,
    moves: Iterable[LargeMove],
    *,
    forecasts: Sequence[ModeForecast],
    decisions: Sequence[DiscoveryDecision],
) -> tuple[MissedMove, ...]:
    """Join each large move to the mode's forecasts and decision records."""
    own_forecasts = [item for item in forecasts if item.mode_id is mode_id]
    own_decisions = [item for item in decisions if item.mode_id is mode_id]
    rows: list[MissedMove] = []
    for move in moves:
        start, end = _entry_window(move)
        window_forecasts = [item for item in own_forecasts if start <= item.as_of < end]
        window_decisions = [item for item in own_decisions if start <= item.as_of < end]
        aligned = [
            item.p_up if move.direction > 0 else 1 - item.p_up
            for item in window_forecasts
        ]
        best = max(aligned, default=None)
        reasons: Counter[ReasonCode] = Counter()
        for decision in window_decisions:
            if decision.decision is not DiscoveryDecisionKind.TRADE:
                reasons.update(decision.reason_codes)
        for forecast in window_forecasts:
            reasons.update(forecast.reason_codes)
        rows.append(
            MissedMove(
                mode_id=mode_id,
                move=move,
                forecasts=len(window_forecasts),
                best_aligned_p=None if best is None else _q(best),
                forecast_direction_agreed=best is not None and best > _HALF,
                traded=any(
                    item.decision is DiscoveryDecisionKind.TRADE
                    for item in window_decisions
                ),
                blocking_reasons=tuple(reasons.most_common()),
            )
        )
    return tuple(rows)


def format_missed_moves(rows_by_mode: Mapping[ModeId, Sequence[MissedMove]]) -> str:
    """Plain-text ledger with a per-mode verdict summary and gate tally."""
    lines: list[str] = []
    for mode_id, rows in rows_by_mode.items():
        verdicts = Counter(row.verdict for row in rows)
        gates: Counter[ReasonCode] = Counter()
        for row in rows:
            if not row.traded:
                gates.update(dict(row.blocking_reasons))
        summary = ", ".join(f"{key}={value}" for key, value in sorted(verdicts.items()))
        lines.append(f"== {mode_id.value}: large_moves={len(rows)} {summary}")
        if gates:
            lines.append(
                "  top blocking reasons: "
                + ", ".join(f"{code.value}={n}" for code, n in gates.most_common(5))
            )
        for row in rows:
            reasons = ",".join(code.value for code, _n in row.blocking_reasons[:3])
            lines.append(
                f"  {row.move.start.isoformat()} dir={row.move.direction:+d} "
                f"move={row.move.move_fraction} atr_x={row.move.atr_multiple} "
                f"{row.verdict} forecasts={row.forecasts} "
                f"best_p={row.best_aligned_p} reasons={reasons or '-'}"
            )
    return "\n".join(lines) if lines else "no large moves detected"
