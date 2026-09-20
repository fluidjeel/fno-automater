"""Quality checks for CAS depth updates."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta

from trading.data.cas_depth.contracts import DepthQualityIssue, NormalizedDepthUpdate

__all__ = ["DepthQualityTracker", "validate_depth_update"]


class DepthQualityTracker:
    """Per-symbol sequence, duplicate and staleness tracking."""

    def __init__(self, *, max_stale_seconds: float) -> None:
        self._max_stale = timedelta(seconds=max_stale_seconds)
        self._last_sequence: dict[str, int] = {}
        self._fingerprints: dict[str, str] = {}
        self._last_exchange_time: dict[str, datetime] = {}
        self._issues: dict[str, list[DepthQualityIssue]] = {}
        self.reconnects = 0
        self.queue_overflows = 0
        self.sequence_gaps = 0
        self.duplicates = 0
        self.stale = 0
        self.malformed = 0
        self.missing_fields = 0

    def record_reconnect(self) -> None:
        self.reconnects += 1

    def record_queue_overflow(self) -> None:
        self.queue_overflows += 1

    def inspect(
        self,
        update: NormalizedDepthUpdate,
        *,
        now: datetime,
    ) -> tuple[DepthQualityIssue, ...]:
        """Return quality issues for one update and update counters."""
        issues: list[DepthQualityIssue] = []
        missing = _missing_required_fields(update)
        if missing:
            issues.append(DepthQualityIssue.MISSING_FIELD)
            self.missing_fields += 1
        if not update.bid_levels and not update.ask_levels:
            issues.append(DepthQualityIssue.MALFORMED)
            self.malformed += 1
        fingerprint = _fingerprint(update)
        prior_fp = self._fingerprints.get(update.symbol)
        if prior_fp == fingerprint:
            issues.append(DepthQualityIssue.DUPLICATE)
            self.duplicates += 1
        self._fingerprints[update.symbol] = fingerprint
        if update.sequence is not None:
            prior_seq = self._last_sequence.get(update.symbol)
            if prior_seq is not None and update.sequence > prior_seq + 1:
                issues.append(DepthQualityIssue.SEQUENCE_GAP)
                self.sequence_gaps += update.sequence - prior_seq - 1
            self._last_sequence[update.symbol] = update.sequence
        last_exchange = self._last_exchange_time.get(update.symbol)
        if last_exchange is not None and update.exchange_timestamp < last_exchange:
            issues.append(DepthQualityIssue.STALE)
            self.stale += 1
        self._last_exchange_time[update.symbol] = update.exchange_timestamp
        age = now - update.exchange_timestamp
        if age > self._max_stale:
            issues.append(DepthQualityIssue.STALE)
            self.stale += 1
        if issues:
            self._issues.setdefault(update.symbol, []).extend(issues)
        return tuple(issues)


def validate_depth_update(
    update: NormalizedDepthUpdate,
) -> tuple[DepthQualityIssue, ...]:
    """Static validation without state."""
    issues: list[DepthQualityIssue] = []
    if _missing_required_fields(update):
        issues.append(DepthQualityIssue.MISSING_FIELD)
    if not update.bid_levels and not update.ask_levels:
        issues.append(DepthQualityIssue.MALFORMED)
    return tuple(issues)


def _missing_required_fields(update: NormalizedDepthUpdate) -> bool:
    if not update.symbol:
        return True
    if not update.bid_levels and not update.ask_levels:
        return True
    return False


def _fingerprint(update: NormalizedDepthUpdate) -> str:
    payload = {
        "symbol": update.symbol,
        "sequence": update.sequence,
        "bids": [(str(level.price), level.quantity) for level in update.bid_levels],
        "asks": [(str(level.price), level.quantity) for level in update.ask_levels],
    }
    return hashlib.blake2b(
        json.dumps(payload, sort_keys=True).encode(),
        digest_size=12,
    ).hexdigest()
