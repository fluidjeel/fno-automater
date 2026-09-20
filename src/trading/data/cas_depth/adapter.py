"""CAS paper adapter: bounded depth-only feature snapshots on disk."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from trading.data.cas_depth.contracts import (
    CAS_DEPTH_FEATURE_SET_VERSION,
    CasDepthFeatureSnapshot,
    DepthQualityIssue,
    NormalizedDepthUpdate,
    TradeAggressor,
)
from trading.data.cas_depth.features import (
    compute_depth_only_features,
    depth_features_complete,
)

__all__ = ["CasDepthPaperAdapter"]


class CasDepthPaperAdapter:
    """Publish bounded depth-only snapshots for paper CAS consumption."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._latest_dir = root / "snapshots" / "latest"
        self._health_path = root / "health.json"
        self._latest_dir.mkdir(parents=True, exist_ok=True)

    def publish(
        self,
        update: NormalizedDepthUpdate,
        prior: NormalizedDepthUpdate | None,
        *,
        quality_issues: tuple[DepthQualityIssue, ...],
        snapshot_id: str,
        now: datetime,
        max_stale_seconds: float,
    ) -> CasDepthFeatureSnapshot:
        """Build and write a bounded feature snapshot; abstain when stale."""
        features = compute_depth_only_features(update, prior)
        age_seconds = (now - update.exchange_timestamp).total_seconds()
        abstain = age_seconds > max_stale_seconds or bool(quality_issues)
        abstain_reason: str | None = None
        if age_seconds > max_stale_seconds:
            abstain_reason = f"stale depth ({age_seconds:.1f}s > {max_stale_seconds}s)"
        elif DepthQualityIssue.MALFORMED in quality_issues:
            abstain_reason = "malformed depth"
        elif DepthQualityIssue.MISSING_FIELD in quality_issues:
            abstain_reason = "missing required fields"
        elif not depth_features_complete(features):
            abstain_reason = "incomplete depth-only features"
        snapshot = CasDepthFeatureSnapshot(
            snapshot_id=snapshot_id,
            symbol=update.symbol,
            feature_set_version=CAS_DEPTH_FEATURE_SET_VERSION,
            exchange_timestamp=update.exchange_timestamp,
            receive_timestamp=update.receive_timestamp,
            calculation_timestamp=now,
            features=features,
            trade_aggressor=TradeAggressor.UNKNOWN,
            quality_issues=quality_issues,
            abstain=abstain,
            abstain_reason=abstain_reason,
        )
        symbol_path = self._latest_dir / f"{update.symbol.replace(':', '_')}.json"
        symbol_path.write_text(
            json.dumps(snapshot.model_dump(mode="json"), indent=2),
            encoding="utf-8",
        )
        self._write_health(snapshot, now=now)
        return snapshot

    def read_latest(self, symbol: str) -> CasDepthFeatureSnapshot | None:
        """Read the latest published snapshot for one symbol."""
        path = self._latest_dir / f"{symbol.replace(':', '_')}.json"
        if not path.is_file():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        return CasDepthFeatureSnapshot.model_validate(payload)

    def should_abstain(self) -> bool:
        """True when the adapter health file marks CAS as abstaining."""
        if not self._health_path.is_file():
            return True
        payload = json.loads(self._health_path.read_text(encoding="utf-8"))
        return bool(payload.get("abstain", True))

    def _write_health(
        self, snapshot: CasDepthFeatureSnapshot, *, now: datetime
    ) -> None:
        self._health_path.write_text(
            json.dumps(
                {
                    "updated_at": now.isoformat(),
                    "cas_data_mode": "DEPTH_ONLY",
                    "cas_live_orders": False,
                    "abstain": snapshot.abstain,
                    "abstain_reason": snapshot.abstain_reason,
                    "symbol": snapshot.symbol,
                    "feature_set_version": snapshot.feature_set_version,
                    "features_complete": snapshot.is_valid,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
