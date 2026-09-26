"""Per-mode drawdown Telegram alerts under DISCOVERY (DISC-A3)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from decimal import Decimal

from trading.config.discovery import DiscoveryConfig
from trading.domain.enums import ModeId
from trading.domain.primitives import Currency, Money
from trading.risk.mode_ledger import FourModeBook

__all__ = ["DrawdownAlertTracker", "drawdown_alert_threshold"]


def drawdown_alert_threshold(discovery: DiscoveryConfig) -> Money:
    """Equity floor that triggers a one-per-day drawdown alert."""
    fraction = Decimal("1") - discovery.books.drawdown_alert_fraction
    amount = discovery.books.starting_equity_per_mode * fraction
    return Money.of(str(amount.quantize(Decimal("0.01"))), Currency.INR)


class DrawdownAlertTracker:
    """Send exactly one Telegram per mode per session day when equity is low."""

    def __init__(self) -> None:
        self._sent: set[tuple[str, str]] = set()

    def check_and_notify(
        self,
        mode_book: FourModeBook,
        discovery: DiscoveryConfig,
        *,
        session_date: date,
        notify: Callable[[str], bool],
    ) -> tuple[ModeId, ...]:
        """Alert modes below the drawdown threshold; never blocks entries."""
        threshold = drawdown_alert_threshold(discovery)
        alerted: list[ModeId] = []
        day_key = session_date.isoformat()
        for mode_id in (
            ModeId.M1_CAS,
            ModeId.M2_DIRECTIONAL,
            ModeId.M3_TACTICAL_POSITIONAL,
            ModeId.M4_STRATEGIC_POSITIONAL,
        ):
            dedupe_key = (mode_id.value, day_key)
            if dedupe_key in self._sent:
                continue
            ledger = mode_book.get_ledger(mode_id)
            if ledger.equity >= threshold:
                continue
            text = (
                f"DISCOVERY drawdown alert: {mode_id.value} equity "
                f"₹{ledger.equity.amount:,.0f} below "
                f"₹{threshold.amount:,.0f} threshold "
                f"({discovery.books.drawdown_alert_fraction:.0%} off "
                f"₹{discovery.books.starting_equity_per_mode:,.0f} book)"
            )
            if notify(text):
                self._sent.add(dedupe_key)
                alerted.append(mode_id)
        return tuple(alerted)
