"""Read-only local operations terminal for the Oracle trading runtime."""

from trading.dashboard.collector import build_dashboard_snapshot
from trading.dashboard.server import DashboardConfig, serve_dashboard

__all__ = ["DashboardConfig", "build_dashboard_snapshot", "serve_dashboard"]
