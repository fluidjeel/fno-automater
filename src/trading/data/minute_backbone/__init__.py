"""Once-per-minute REST snapshots for platter pipeline."""

from trading.data.minute_backbone.scheduler import run_minute_backbone_once

__all__ = ["run_minute_backbone_once"]
