"""Telegram alerts for consecutive PAPER data-feed builder failures (DISC-A10)."""

from __future__ import annotations

from collections.abc import Callable
from http import HTTPStatus

from trading.data.fyers.client import FyersApiError, fyers_http_status

__all__ = ["DataFeedAlertTracker", "fyers_http_status"]

_FAILURE_ALERT_THRESHOLD = 3


class DataFeedAlertTracker:
    """Alert once after three builder failures and once when the feed recovers."""

    def __init__(self) -> None:
        self._consecutive = 0
        self._failure_alert_sent = False

    @property
    def consecutive_failures(self) -> int:
        """Number of consecutive builder failures without a successful cycle."""
        return self._consecutive

    def record_failure(
        self,
        exc: BaseException,
        *,
        notify: Callable[[str], bool],
    ) -> bool:
        """Increment the streak and alert once when it reaches three failures."""
        self._consecutive += 1
        if self._consecutive != _FAILURE_ALERT_THRESHOLD or self._failure_alert_sent:
            return False
        if notify(_failure_alert_text(exc)):
            self._failure_alert_sent = True
            return True
        return False

    def record_success(self, *, notify: Callable[[str], bool]) -> bool:
        """Reset the streak and send one recovery alert after a prior failure alert."""
        recovered = self._failure_alert_sent
        if recovered:
            notify("DATA feed recovered: builder succeeded after consecutive failures.")
        self._consecutive = 0
        self._failure_alert_sent = False
        return recovered


def _failure_alert_text(exc: BaseException) -> str:
    if (
        isinstance(exc, FyersApiError)
        and fyers_http_status(exc) == HTTPStatus.UNAUTHORIZED
    ):
        return "Fyers token expired — refresh token"
    return (
        "PAPER data feed error: builder failed 3 consecutive cycles "
        f"({type(exc).__name__}: {exc})"
    )
