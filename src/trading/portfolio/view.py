"""Read-only portfolio view for Layer 3 strategies."""

from __future__ import annotations

from trading.domain.contracts.portfolio import PortfolioSnapshot, PortfolioView
from trading.domain.enums import SystemState

__all__ = ["build_portfolio_view"]


def build_portfolio_view(
    snapshot: PortfolioSnapshot,
    *,
    system_state: SystemState,
    entries_blocked: bool,
) -> PortfolioView:
    """Derive the strategy-visible view from authoritative portfolio truth."""
    entries_permitted = system_state.permits_new_exposure and not entries_blocked
    return PortfolioView.from_snapshot(
        snapshot,
        system_state=system_state,
        entries_permitted=entries_permitted,
    )
