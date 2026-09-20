"""ADESK-A9: versioned delta packets, prefix stability, delta_gap_rate."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from trading.ai.packets import (
    CACHE_BOUNDARY,
    PACKET_VERSION,
    DeltaGapCounter,
    InvalidationSnapshot,
    PrefixSpec,
    build_delta_packet,
    build_prompt_prefix,
    packet_to_canonical_json,
    prefix_hash,
)
from trading.domain.enums import DeskRole

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "packets"
NOW = datetime(2026, 9, 20, 10, 30, tzinfo=UTC)


def test_packet_version_and_golden_fixture() -> None:
    packet = build_delta_packet(
        role=DeskRole.POSITION,
        as_of=NOW,
        snapshot_id="snap-1",
        trade_id="tr-1",
        spot=Decimal("24500.5"),
        iv_percentile=Decimal("62"),
        unrealized_r=Decimal("0.35"),
        invalidations=(
            InvalidationSnapshot(
                condition_id="INV-1",
                metric="MAE_R",
                status="HOLDING",
                distance=Decimal("0.44"),
            ),
        ),
    )
    assert packet.packet_version == PACKET_VERSION
    golden = (FIXTURES / "position_delta_v1.json").read_text().strip()
    assert packet_to_canonical_json(packet) == golden


def test_prefix_hash_is_stable() -> None:
    prefix = build_prompt_prefix(
        PrefixSpec(
            system_role="You are the POSITION desk.",
            authority_rules="Authority: SHADOW only.",
            decision_policy="Falsify the thesis; do not invent entries.",
            tool_definitions="tools: none in L1",
            output_schema="JSON AgentDecision",
        )
    )
    assert CACHE_BOUNDARY in prefix
    expected = (FIXTURES / "prefix_v1.sha256").read_text().strip()
    assert prefix_hash(prefix) == expected
    assert prefix == (FIXTURES / "prefix_v1.txt").read_text()


def test_delta_gap_rate_counts_packet_field_refetches() -> None:
    counter = DeltaGapCounter(packet_fields={"spot", "iv_percentile", "unrealized_r"})
    counter.record_tool_fetch("spot")  # gap
    counter.record_tool_fetch("order_book")  # not a packet field
    counter.record_tool_fetch("iv_percentile")  # gap
    assert counter.gap_count == 2
    assert counter.fetch_count == 3
    assert counter.delta_gap_rate == Decimal("0.6667")
