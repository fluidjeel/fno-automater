"""Mode B pre-close early exit rule."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from trading.config.m2_early_exit import M2EarlyExitConfig, M2EarlyExitPolicy
from trading.domain.primitives import Currency, Money
from trading.trade.m2_pre_close_early_exit import evaluate_m2_pre_close_early_exit


def _cfg() -> M2EarlyExitPolicy:
    return M2EarlyExitPolicy(
        enabled=True,
        early_exit=M2EarlyExitConfig(min_loss_fraction_of_max_loss=Decimal("0.75")),
    )


def test_early_exit_fires_in_window_when_loss_deep() -> None:
    now = datetime(2026, 10, 22, 9, 15, tzinfo=UTC)  # 14:45 IST
    ok, _ = evaluate_m2_pre_close_early_exit(
        unrealized_pnl=Money(Decimal("-8000"), Currency.INR),
        max_loss=Money(Decimal("10000"), Currency.INR),
        now=now,
        config=_cfg(),
    )
    assert ok is True


def test_early_exit_skips_outside_window() -> None:
    now = datetime(2026, 10, 22, 5, 0, tzinfo=UTC)  # 10:30 IST
    ok, _ = evaluate_m2_pre_close_early_exit(
        unrealized_pnl=Money(Decimal("-9000"), Currency.INR),
        max_loss=Money(Decimal("10000"), Currency.INR),
        now=now,
        config=_cfg(),
    )
    assert ok is False
