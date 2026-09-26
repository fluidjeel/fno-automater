"""Tests for Gate G1: One-Lot Feasibility and Affordability Check."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from trading.config.risk_policy import load_risk_policy_text
from trading.domain.enums import ModeId, ReasonCode, SizingBindingConstraint
from trading.domain.primitives import Currency, Money
from trading.risk.affordability import (
    AffordabilityStatus,
    ChainProvenance,
    evaluate_affordability,
    load_latest_option_chain,
    load_nifty_instrument_spec,
    persist_affordability_report,
    render_markdown_report,
)


@pytest.fixture
def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _affordability_data_available(repo_root: Path) -> bool:
    instrument_store = repo_root / "data" / "reference" / "instruments" / "NSE_FO.jsonl"
    fyers_dir = repo_root / "data" / "raw" / "fyers"
    has_chain = any(fyers_dir.glob("202*/*.json")) if fyers_dir.is_dir() else False
    return instrument_store.is_file() and has_chain


@pytest.fixture
def repo_root_with_data(repo_root: Path) -> Path:
    if not _affordability_data_available(repo_root):
        pytest.skip("affordability reference data not available in this environment")
    return repo_root


def test_load_instrument_spec_dynamic_lot_size(repo_root_with_data: Path) -> None:
    """Lot size, tick size, and source path must be loaded dynamically from the contract file."""
    lot_size, tick_size, source_path = load_nifty_instrument_spec(repo_root_with_data)
    assert lot_size == 65
    assert tick_size == Decimal("0.05")
    assert "NSE_FO.jsonl" in source_path


def test_latest_chain_provenance_and_offline_disclaimer(
    repo_root_with_data: Path,
) -> None:
    """Option chain must be loaded with dynamic timestamp provenance and marked offline."""
    raw_chain, provenance = load_latest_option_chain(repo_root_with_data)
    assert "payload" in raw_chain
    assert provenance.capture_id != ""
    assert provenance.provider == "fyers"
    assert provenance.is_live_probe is False
    assert provenance.market_status == "CLOSED_OFFLINE_SNAPSHOT"
    assert provenance.underlying_symbol == "NSE:NIFTY50-INDEX"
    assert provenance.underlying_spot > Decimal(0)
    assert provenance.expiry_date != ""
    assert provenance.event_time != ""
    assert provenance.receive_time != ""


def test_evaluate_affordability_all_modes_and_families(
    repo_root_with_data: Path,
) -> None:
    """Evaluate all 18 mode × allowed family structures using the ₹7L book and 65-contract lot."""
    report = evaluate_affordability(repo_root_with_data)
    assert report.total_equity == Money.of("700000", Currency.INR)
    assert report.lot_size == 65
    assert len(report.evaluations) == 18
    assert report.charges_per_lot_source == "config/risk.yaml"
    assert report.slippage_buffer_fraction == Decimal("0.0025")

    # Check mode capital specs (§10.1)
    m1_spec = report.mode_specs[ModeId.M1_CAS]
    assert m1_spec.reference_capital == Money.of("70000", Currency.INR)
    assert m1_spec.per_trade_cap == Money.of("3500", Currency.INR)

    m2_spec = report.mode_specs[ModeId.M2_DIRECTIONAL]
    assert m2_spec.reference_capital == Money.of("196000", Currency.INR)
    assert m2_spec.per_trade_cap == Money.of("7840", Currency.INR)

    m3_spec = report.mode_specs[ModeId.M3_TACTICAL_POSITIONAL]
    assert m3_spec.reference_capital == Money.of("210000", Currency.INR)
    assert m3_spec.per_trade_cap == Money.of("4200", Currency.INR)

    m4_spec = report.mode_specs[ModeId.M4_STRATEGIC_POSITIONAL]
    assert m4_spec.reference_capital == Money.of("224000", Currency.INR)
    assert m4_spec.per_trade_cap == Money.of("2240", Currency.INR)

    # Check Mode 1 (CAS): moderately OTM options fit within ₹3,500
    m1_evals = [e for e in report.evaluations if e.mode_id == ModeId.M1_CAS]
    assert len(m1_evals) == 2
    assert all(e.status is AffordabilityStatus.AFFORDABLE for e in m1_evals)
    assert all(e.one_lot_fits_budget is True for e in m1_evals)
    assert all(e.reason_code is ReasonCode.OK for e in m1_evals)

    # Mode 2 uses the revised cap. The checked-in chain must fit without leaving the delta band.
    m2_evals = [e for e in report.evaluations if e.mode_id == ModeId.M2_DIRECTIONAL]
    assert len(m2_evals) == 2
    assert all(e.status is AffordabilityStatus.AFFORDABLE for e in m2_evals)
    assert all(e.one_lot_fits_budget is True for e in m2_evals)
    assert all(e.reason_code is ReasonCode.OK for e in m2_evals)

    # Check Mode 3 (Tactical Positional Spreads): all 4 verticals fit ₹4,200
    m3_evals = [
        e for e in report.evaluations if e.mode_id == ModeId.M3_TACTICAL_POSITIONAL
    ]
    assert len(m3_evals) == 4
    assert all(e.status is AffordabilityStatus.AFFORDABLE for e in m3_evals)
    assert all(e.one_lot_fits_budget is True for e in m3_evals)

    # Check Mode 4 (Strategic Positional Basket)
    m4_evals = {
        e.family_id: e
        for e in report.evaluations
        if e.mode_id == ModeId.M4_STRATEGIC_POSITIONAL
    }
    assert len(m4_evals) == 10

    # Verticals and condor/butterflies fit under ₹2,800
    assert m4_evals["bull_call_debit"].status is AffordabilityStatus.AFFORDABLE
    assert m4_evals["bear_put_debit"].status is AffordabilityStatus.AFFORDABLE
    assert m4_evals["bull_put_credit"].status is AffordabilityStatus.AFFORDABLE
    assert m4_evals["bear_call_credit"].status is AffordabilityStatus.AFFORDABLE
    assert (
        m4_evals["short_iron_condor_defined"].status is AffordabilityStatus.AFFORDABLE
    )
    assert (
        m4_evals["short_iron_butterfly_defined"].status
        is AffordabilityStatus.AFFORDABLE
    )
    assert m4_evals["long_call_butterfly"].status is AffordabilityStatus.AFFORDABLE
    assert m4_evals["long_put_butterfly"].status is AffordabilityStatus.AFFORDABLE

    # Straddle and strangle exceed ₹2,800 cap
    assert (
        m4_evals["long_straddle"].status is AffordabilityStatus.MIN_LOT_EXCEEDS_BUDGET
    )
    assert m4_evals["long_straddle"].one_lot_fits_budget is False
    assert m4_evals["long_straddle"].reason_code is ReasonCode.MIN_LOT_EXCEEDS_BUDGET
    assert m4_evals["long_straddle"].binding_constraint is SizingBindingConstraint.RISK

    assert (
        m4_evals["long_strangle"].status is AffordabilityStatus.MIN_LOT_EXCEEDS_BUDGET
    )
    assert m4_evals["long_strangle"].one_lot_fits_budget is False
    assert m4_evals["long_strangle"].reason_code is ReasonCode.MIN_LOT_EXCEEDS_BUDGET
    assert m4_evals["long_strangle"].binding_constraint is SizingBindingConstraint.RISK


def test_fixture_lot_above_cap_abstains(repo_root_with_data: Path) -> None:
    """A fixture lot size above the cap causes all structures to abstain with MIN_LOT_EXCEEDS_BUDGET."""
    report = evaluate_affordability(repo_root_with_data, lot_size_override=1000)
    assert report.lot_size == 1000
    assert len(report.evaluations) == 18

    for ev in report.evaluations:
        assert ev.status is AffordabilityStatus.MIN_LOT_EXCEEDS_BUDGET
        assert ev.one_lot_fits_budget is False
        assert ev.reason_code is ReasonCode.MIN_LOT_EXCEEDS_BUDGET
        assert ev.binding_constraint in {
            SizingBindingConstraint.RISK,
            SizingBindingConstraint.CAPITAL,
        }
        assert ev.total_cost_per_lot.amount > ev.per_trade_cap.amount


def test_m4_straddle_and_strangle_exceed_budget(repo_root_with_data: Path) -> None:
    """Mode 4 straddle and strangle defined risk exceeds the 1% cap (₹2,800)."""
    report = evaluate_affordability(repo_root_with_data)
    m4_evals = {
        e.family_id: e
        for e in report.evaluations
        if e.mode_id == ModeId.M4_STRATEGIC_POSITIONAL
    }

    straddle = m4_evals["long_straddle"]
    assert straddle.status is AffordabilityStatus.MIN_LOT_EXCEEDS_BUDGET
    assert straddle.total_cost_per_lot.amount > Decimal("10000")
    assert straddle.per_trade_cap == Money.of("2240", Currency.INR)

    strangle = m4_evals["long_strangle"]
    assert strangle.status is AffordabilityStatus.MIN_LOT_EXCEEDS_BUDGET
    assert strangle.total_cost_per_lot.amount > Decimal("3500")
    assert strangle.total_cost_per_lot.amount > strangle.per_trade_cap.amount
    assert strangle.per_trade_cap == Money.of("2240", Currency.INR)


def test_isolated_synthetic_chain_unit_test() -> None:
    """Isolated in-memory unit test: verifies scanning and affordability without disk files."""
    dummy_repo = Path("/nonexistent")
    synthetic_chain = {
        "capture_id": "test-synthetic-01",
        "provider": "synthetic",
        "received_at": "2026-09-24T10:00:00Z",
        "payload": {
            "data": {
                "timestamp": 1790244000,
                "expiryData": [{"date": "29-09-2026"}],
                "optionsChain": [
                    {
                        "symbol": "NSE:NIFTY50-INDEX",
                        "strike_price": -1,
                        "ltp": 23350.0,
                    },
                    # ATM strikes (23350)
                    {
                        "strike_price": 23350,
                        "option_type": "CE",
                        "ask": 50.0,
                        "bid": 49.5,
                        "greeks": {"delta": 0.50},
                    },
                    {
                        "strike_price": 23350,
                        "option_type": "PE",
                        "ask": 50.0,
                        "bid": 49.5,
                        "greeks": {"delta": -0.50},
                    },
                    # OTM call (23400)
                    {
                        "strike_price": 23400,
                        "option_type": "CE",
                        "ask": 20.0,
                        "bid": 19.5,
                        "greeks": {"delta": 0.28},
                    },
                    # OTM put (23300)
                    {
                        "strike_price": 23300,
                        "option_type": "PE",
                        "ask": 20.0,
                        "bid": 19.5,
                        "greeks": {"delta": -0.28},
                    },
                    # Deep OTM call (23450)
                    {
                        "strike_price": 23450,
                        "option_type": "CE",
                        "ask": 10.0,
                        "bid": 9.5,
                        "greeks": {"delta": 0.15},
                    },
                    # Deep OTM put (23250)
                    {
                        "strike_price": 23250,
                        "option_type": "PE",
                        "ask": 10.0,
                        "bid": 9.5,
                        "greeks": {"delta": -0.15},
                    },
                ],
            }
        },
    }

    mock_provenance = ChainProvenance(
        capture_id="test-synthetic-01",
        source_file="synthetic_memory",
        provider="synthetic",
        event_time="2026-09-24T10:00:00Z",
        receive_time="2026-09-24T10:00:00Z",
        is_live_probe=False,
        market_status="SYNTHETIC_TEST",
        underlying_symbol="NSE:NIFTY50-INDEX",
        underlying_spot=Decimal("23350.0"),
        expiry_date="29-09-2026",
    )

    raw_risk_yaml = """
schema_version: "1"
policy_version: "6"
strategy_allocations:
  positional_long_option:
    allocation_fraction: "0.20"
underlying_concentration_fraction: "0.25"
options_premium_budget_fraction: "0.15"
slippage_buffer_fraction: "0.0025"
charges_per_lot:
  amount: "50"
  currency: INR
net_delta_limit: 500
net_vega_limit: "5000"
expiry_day_notional_fraction: "0.35"
directional_agreement_max: "0.85"
single_event_exposure_fraction: "0.25"
tail_budget_fraction: "0.08"
decision_ttl_seconds: 300
"""
    loaded_policy = load_risk_policy_text(raw_risk_yaml)

    # Evaluate with 65-lot size
    report = evaluate_affordability(
        dummy_repo,
        lot_size_override=65,
        total_equity=Money.of("700000", Currency.INR),
        chain_override=synthetic_chain,
        risk_policy_override=loaded_policy.config,
        provenance_override=mock_provenance,
    )

    assert len(report.evaluations) == 18
    assert report.provenance.market_status == "SYNTHETIC_TEST"
    assert report.lot_size == 65

    # M1 CAS: scans 0.20-0.35 delta -> picks 23400 CE (ask 20.0) -> cost 20*65 = 1300 + slippage + 50 = ~1353 <= 3500 -> AFFORDABLE
    m1_call = next(
        e
        for e in report.evaluations
        if e.mode_id == ModeId.M1_CAS and e.family_id == "long_call"
    )
    assert m1_call.status is AffordabilityStatus.AFFORDABLE
    assert m1_call.one_lot_fits_budget is True

    # M2 Directional: scans 0.45-0.65 delta -> picks 23350 CE (ask 50.0) -> cost 50*65 = 3250 + slippage + 50 = ~3308 <= 4200 -> AFFORDABLE in this low-premium chain!
    m2_call = next(
        e
        for e in report.evaluations
        if e.mode_id == ModeId.M2_DIRECTIONAL and e.family_id == "long_call"
    )
    assert m2_call.status is AffordabilityStatus.AFFORDABLE
    assert m2_call.one_lot_fits_budget is True


def test_markdown_report_rendering_and_persistence(
    repo_root_with_data: Path, tmp_path: Path
) -> None:
    """Render markdown report and test persistence to disk."""
    report = evaluate_affordability(repo_root_with_data)
    rendered = render_markdown_report(report)

    assert "# Gate G1 — One-Lot Feasibility and Affordability Report" in rendered
    assert "Offline Feasibility Only — NOT Permission to Run PAPER" in rendered
    assert "M1_CAS" in rendered
    assert "M2_DIRECTIONAL" in rendered
    assert "M3_TACTICAL_POSITIONAL" in rendered
    assert "M4_STRATEGIC_POSITIONAL" in rendered
    assert "MIN_LOT_EXCEEDS_BUDGET" in rendered
    assert "AFFORDABLE" in rendered
    assert "Do NOT cut delta to force a pass" in rendered

    out_file = tmp_path / "test_g1_report.md"
    saved = persist_affordability_report(repo_root_with_data, report, out_file)
    assert saved.exists()
    assert saved.read_text(encoding="utf-8") == rendered
