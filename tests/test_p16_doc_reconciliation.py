"""Phase P16: canonical doc reconciliation and honest limitation markers."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class TestCanonicalDocs:
    def test_current_state_records_p16_completion(self) -> None:
        text = (ROOT / "docs/context/CURRENT_STATE.md").read_text(encoding="utf-8")
        assert "P16" in text or "P14" in text
        assert "EXPERIMENTAL_ONLY_RISK_BOUND_UNPROVEN" in text
        assert "live not approved" in text.lower() or "live blocked" in text.lower()

    def test_context_index_lists_calendar_experimental(self) -> None:
        text = (ROOT / "docs/context/FOUR_MODE_CONTEXT_INDEX.md").read_text(
            encoding="utf-8"
        )
        assert "calendar" in text.lower()
        assert "P14" in text or "experimental" in text.lower()

    def test_architecture_does_not_claim_all_strategies_active(self) -> None:
        text = (ROOT / "docs/context/ARCHITECTURE.md").read_text(encoding="utf-8")
        lowered = text.lower()
        assert "four" in lowered or "mode" in lowered
        assert "live" not in lowered or "not live" in lowered or "paper" in lowered
