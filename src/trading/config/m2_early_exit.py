"""Optional M2 pre-close early exit window (disabled by default on M2 branch)."""

from __future__ import annotations

from datetime import time
from decimal import Decimal

from pydantic import Field

from trading.domain.contracts.base import ExactDecimal, StrictModel

__all__ = [
    "M2EarlyExitConfig",
    "M2EarlyExitPolicy",
    "early_exit_window",
]


class M2EarlyExitConfig(StrictModel):
    """Loss fraction of max-loss that triggers exit in the pre-close window."""

    min_loss_fraction_of_max_loss: ExactDecimal = Field(
        default=Decimal("0.75"),
        gt=Decimal("0"),
        le=Decimal("1"),
    )
    window_start_ist: time = time(14, 45)
    window_end_ist: time = time(15, 15)


class M2EarlyExitPolicy(StrictModel):
    """Feature flag plus parameters for pre-close M2 exits."""

    enabled: bool = False
    early_exit: M2EarlyExitConfig = Field(default_factory=M2EarlyExitConfig)


def early_exit_window(config: M2EarlyExitPolicy) -> tuple[time, time]:
    """Return inclusive IST window bounds from config."""
    return config.early_exit.window_start_ist, config.early_exit.window_end_ist
