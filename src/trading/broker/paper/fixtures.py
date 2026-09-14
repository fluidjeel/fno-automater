"""Load sanitized paper-broker fixtures for offline tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from trading.broker.ports import BrokerFunds, MarginPreviewResult
from trading.domain.contracts.portfolio import PositionRecord

__all__ = ["PaperBrokerFixtures"]


class PaperBrokerFixtures:
    """Sanitized broker state loaded from tests/fixtures/broker/."""

    def __init__(
        self,
        *,
        account: BrokerFunds,
        positions: tuple[PositionRecord, ...],
        margin_previews: dict[str, MarginPreviewResult],
    ) -> None:
        self.account = account
        self.positions = positions
        self.margin_previews = margin_previews

    @classmethod
    def load(cls, root: Path) -> PaperBrokerFixtures:
        """Load account, positions and margin preview fixtures from root."""
        account = BrokerFunds.model_validate(
            _read_json(root / "account_state.json")
        )
        positions_raw = _read_json(root / "positions.json")
        if not isinstance(positions_raw, list):
            raise ValueError("positions.json must contain a JSON array")
        positions = tuple(
            PositionRecord.model_validate(row) for row in positions_raw
        )
        previews_raw = _read_json(root / "margin_preview.json")
        if not isinstance(previews_raw, dict):
            raise ValueError("margin_preview.json must contain a JSON object")
        entries = previews_raw.get("previews", [])
        if not isinstance(entries, list):
            raise ValueError("margin_preview.json previews must be a JSON array")
        margin_previews = {
            str(row["key"]): MarginPreviewResult.model_validate(row["result"])
            for row in entries
            if isinstance(row, dict) and "key" in row and "result" in row
        }
        return cls(
            account=account,
            positions=positions,
            margin_previews=margin_previews,
        )


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))
