"""CAS paper adapter: bounded depth-only feature snapshots on disk."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

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

_ABSTAIN_QUALITY_ISSUES = frozenset(
    {
        DepthQualityIssue.MALFORMED,
        DepthQualityIssue.MISSING_FIELD,
        DepthQualityIssue.STALE,
    }
)


class CasDepthPaperAdapter:
    """Publish bounded depth-only snapshots for paper CAS consumption."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._latest_dir = root / "snapshots" / "latest"
        self._health_path = root / "health.json"
        self._latest_dir.mkdir(parents=True, exist_ok=True)
        self._symbol_health: dict[str, dict[str, Any]] = {}
        if self._health_path.is_file():
            self._load_existing_health()

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
        blocking_issues = [
            issue for issue in quality_issues if issue in _ABSTAIN_QUALITY_ISSUES
        ]
        abstain = age_seconds > max_stale_seconds or bool(blocking_issues)
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

    def _load_existing_health(self) -> None:
        payload = json.loads(self._health_path.read_text(encoding="utf-8"))
        symbols = payload.get("symbols")
        if isinstance(symbols, dict):
            self._symbol_health = {
                str(key): dict(value)
                for key, value in symbols.items()
                if isinstance(value, dict)
            }

    def _write_health(
        self, snapshot: CasDepthFeatureSnapshot, *, now: datetime
    ) -> None:
        symbol_key = snapshot.symbol.replace(":", "_")
        self._symbol_health[symbol_key] = {
            "symbol": snapshot.symbol,
            "updated_at": now.isoformat(),
            "exchange_timestamp": snapshot.exchange_timestamp.isoformat(),
            "abstain": snapshot.abstain,
            "abstain_reason": snapshot.abstain_reason,
            "features_complete": snapshot.is_valid,
        }
        any_abstain = any(
            entry.get("abstain", True) for entry in self._symbol_health.values()
        )
        all_complete = all(
            entry.get("features_complete", False)
            for entry in self._symbol_health.values()
        )
        self._health_path.write_text(
            json.dumps(
                {
                    "updated_at": now.isoformat(),
                    "cas_data_mode": "DEPTH_ONLY",
                    "cas_live_orders": False,
                    "abstain": any_abstain,
                    "abstain_reason": snapshot.abstain_reason if any_abstain else None,
                    "symbol": snapshot.symbol,
                    "feature_set_version": snapshot.feature_set_version,
                    "features_complete": all_complete and not any_abstain,
                    "symbols": self._symbol_health,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
