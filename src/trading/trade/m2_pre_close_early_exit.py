"""Mode B pre-close early exit before the M2 carry gate."""

from __future__ import annotations

from datetime import datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

from trading.config.m2_ab_study import M2AbStudyConfig, early_exit_window
from trading.domain.primitives import Money

IST = ZoneInfo("Asia/Kolkata")

__all__ = ["evaluate_m2_pre_close_early_exit"]


def evaluate_m2_pre_close_early_exit(
    *,
    unrealized_pnl: Money,
    max_loss: Money,
    now: datetime,
    config: M2AbStudyConfig,
) -> tuple[bool, str]:
    """Return whether Mode B should shadow-exit before the carry gate."""
    if not config.enabled:
        return False, ""
    start, end = early_exit_window(config)
    local_t = now.astimezone(IST).time()
    if local_t < start or local_t > end:
        return False, ""
    if unrealized_pnl.amount >= Decimal(0):
        return False, ""
    loss = (-unrealized_pnl.amount).quantize(Decimal("0.01"))
    threshold = (max_loss.amount * config.early_exit.min_loss_fraction_of_max_loss).quantize(
        Decimal("0.01")
    )
    if loss < threshold:
        return False, ""
    detail = (
        f"pre-close early exit: loss {loss} >= "
        f"{config.early_exit.min_loss_fraction_of_max_loss} * max_loss {max_loss.amount}"
    )
    return True, detail
