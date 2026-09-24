"""Tests for Gate G3: CAS Field Inventory, Mechanism Disambiguation, and Entry Latency."""

from __future__ import annotations

from datetime import UTC, datetime, time
from pathlib import Path

import pytest
import yaml

from trading.data.cas_depth.contracts import (
    CAS_DEPTH_FEATURE_KEYS,
    CAS_DEPTH_FEATURE_SET_VERSION,
    NormalizedDepthUpdate,
    TradeAggressor,
)
from trading.data.cas_features import CAS_FEATURE_KEYS, CAS_FEATURE_SET_VERSION
from trading.data.fyers.capability_probe import CapturedMessage, analyze_samples
from trading.runtime.protection import ProtectionConfig
from trading.strategies.cas_microstructure import (
    CAS_HOLDING_SECONDS,
    CAS_WINDOW_END_IST,
    CAS_WINDOW_START_IST,
)


@pytest.fixture
def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def test_g3_aggressor_side_strictly_unavailable() -> None:
    """Capability probe and normalized depth must strictly report aggressor side unavailable."""
    now = datetime(2026, 9, 24, 10, 0, tzinfo=UTC)
    sample = CapturedMessage(
        feed="data_ws",
        data_type="DepthUpdate",
        symbol="NSE:NIFTY26SEP24500CE",
        receive_time=now,
        payload={
            "bid_price": [100.0],
            "ask_price": [100.5],
            "bid_qty": [50],
            "ask_qty": [40],
        },
        message_kind="depth",
    )
    summary = analyze_samples(
        (sample,),
        feed="data_ws",
        data_type="DepthUpdate",
        symbol="NSE:NIFTY26SEP24500CE",
    )
    assert summary.has_aggressor_side is False
    assert any("aggressor_side unavailable" in note for note in summary.notes)

    # Normalized depth update contract must default to UNKNOWN aggressor
    depth_update = NormalizedDepthUpdate(
        symbol="NSE:NIFTY26SEP24500CE",
        exchange_timestamp=now,
        receive_timestamp=now,
        bid_levels=(),
        ask_levels=(),
        feed="data_ws",
    )
    assert depth_update.trade_aggressor is TradeAggressor.UNKNOWN


def test_g3_quote_only_vs_depth_only_profiles_are_distinct() -> None:
    """Quote-only (cas-microstructure-v1) and depth-only (cas-depth-only-v1) must be disjoint."""
    assert CAS_FEATURE_SET_VERSION == "cas-microstructure-v1"
    assert CAS_DEPTH_FEATURE_SET_VERSION == "cas-depth-only-v1"
    assert CAS_FEATURE_SET_VERSION != CAS_DEPTH_FEATURE_SET_VERSION

    # Key sets must be explicitly distinct
    quote_keys = set(CAS_FEATURE_KEYS)
    depth_keys = set(CAS_DEPTH_FEATURE_KEYS)
    assert len(quote_keys) == 4
    assert len(depth_keys) == 8
    # No key collision between the two feature profiles
    assert quote_keys.isdisjoint(depth_keys)


def test_g3_entry_poll_vs_protection_poll_latency_separation(repo_root: Path) -> None:
    """Session entry loop is 60s poll; protection loop is 2s REST fallback (open positions only)."""
    session_config_path = repo_root / "config" / "paper_session.yaml"
    with session_config_path.open("r", encoding="utf-8") as f:
        session_config = yaml.safe_load(f)

    entry_poll_seconds = session_config["poll_interval_seconds"]
    assert entry_poll_seconds == 60

    # Protection config default REST fallback is 2 seconds
    prot_config = ProtectionConfig()
    assert prot_config.rest_poll_seconds == 2
    assert prot_config.quote_max_age_ms == 5000

    # Microstructure holding horizon is 900 seconds
    assert CAS_HOLDING_SECONDS == 900

    # 60s entry poll represents 6.67% of the trade lifetime
    latency_ratio = entry_poll_seconds / CAS_HOLDING_SECONDS
    assert pytest.approx(latency_ratio, 0.001) == 60 / 900

    # Entry poll is 30x slower than the protection poll fallback
    assert entry_poll_seconds == 30 * prot_config.rest_poll_seconds


def test_g3_mechanism_separation_nfo_continuous_vs_cash_cas(repo_root: Path) -> None:
    """Mode 1 trades the NFO continuous market only; NSE cash CAS is 15:30-15:40.

    The authoritative M1 schedule is continuous 09:20-15:00 plus closing context
    15:00-15:25, matching CasEventDrivenConfig.scan_windows. M1's own
    microstructure signal is therefore never the exchange's cash-market Closing
    Auction Session, which begins at 15:30.
    """
    assert time(9, 20) == CAS_WINDOW_START_IST
    assert time(15, 25) == CAS_WINDOW_END_IST
    # The cash auction starts at 15:30, strictly after the M1 window closes.
    assert CAS_WINDOW_END_IST < time(15, 30)

    session_config_path = repo_root / "config" / "paper_session.yaml"
    with session_config_path.open("r", encoding="utf-8") as f:
        session_config = yaml.safe_load(f)

    # Session EOD is 15:40 (accommodating post-close cash settlement), not option trading
    assert session_config["eod_local"] == "15:40"


def test_g3_report_file_exists_and_records_blocked_paper_stance(
    repo_root: Path,
) -> None:
    """Canonical Gate G3 report exists and documents the formal BLOCKED verdict."""
    report_path = repo_root / "docs" / "reports" / "G3_CAS_FIELD_AND_LATENCY_REPORT.md"
    assert report_path.is_file(), f"Report missing at {report_path}"

    content = report_path.read_text(encoding="utf-8")
    assert "Gate G3" in content
    assert "BLOCKED" in content
    assert "aggressor_side" in content
    assert "cas-microstructure-v1" in content
    assert "cas-depth-only-v1" in content
    assert "EXP-M1-QUOTE" in content
    assert "EXP-M1-DEPTH" in content
    assert "60-second" in content
    assert "2-second" in content
    assert "Review Amendments & Operational Enforcement Clarifications" in content
    assert "EXTERNAL_VERIFICATION_REQUIRED" in content
    assert "PLANNED_IN_P1" in content


def test_g3_policy_blocked_vs_legacy_config_enforcement(repo_root: Path) -> None:
    """Shipped four-mode config keeps M1 CAS in SHADOW until event path is enabled."""
    session_config_path = repo_root / "config" / "paper_session.yaml"
    with session_config_path.open("r", encoding="utf-8") as f:
        session_config = yaml.safe_load(f)

    assert session_config["routing_profile"] == "four_mode"
    assert session_config["mode_stances"]["M1_CAS"] == "PAPER"
    assert session_config["mode_stances"]["M2_DIRECTIONAL"] == "PAPER"
    assert session_config["cas_event_driven"]["enabled"] is True
    assert session_config["cas_event_driven"]["quote_max_age_ms"] == 500
    assert session_config["cas_event_driven"]["max_entry_latency_ms"] == 2000
    legacy_path = repo_root / "config" / "paper_session_legacy.yaml"
    with legacy_path.open("r", encoding="utf-8") as f:
        legacy_config = yaml.safe_load(f)
    assert legacy_config["routing_profile"] == "legacy"
    assert legacy_config["strategy_stances"]["cas_microstructure"] == "SHADOW"
