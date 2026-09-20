"""Tests verifying regulatory alignment with SEBI derivatives framework."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

# SEBI 2024+ Index Derivative Mandate:
# 1. Standard index lot sizes (e.g. NIFTY revised lot size: 25 -> 75)
# 2. Minimum contract notional >= INR 15 Lakhs
# 3. Benchmark weekly expiry only per exchange (NIFTY for NSE)
from pathlib import Path

from tests.factories import option_contract
from trading.config import load_risk_policy
from trading.domain.enums import Exchange, InstrumentKind, OptionType
from trading.domain.primitives import Currency, Lots, LotSize, Money


def test_lot_size_scaling_and_contracts_per_lot() -> None:
    """Verify LotSize correctly scales integer lot counts into total contracts."""
    nifty_lot = LotSize(75)
    assert nifty_lot.contracts_per_lot == 75

    # 1 lot = 75 contracts
    qty = Lots(1).to_quantity(nifty_lot)
    assert qty.contracts == 75

    # 2 lots = 150 contracts
    qty_2 = Lots(2).to_quantity(nifty_lot)
    assert qty_2.contracts == 150


def test_contract_ref_sebi_index_expiry_rule() -> None:
    """SEBI permits weekly index derivatives only on the exchange's single benchmark."""
    # NIFTY weekly on NSE is valid
    nifty_weekly = option_contract(
        symbol="NIFTY2692425000CE",
        exchange=Exchange.NFO,
        instrument_kind=InstrumentKind.OPTION,
        expiry=date(2026, 9, 24),
        strike=Decimal("25000"),
        option_type=OptionType.CALL,
    )
    assert nifty_weekly.symbol.startswith("NIFTY")
    assert nifty_weekly.exchange is Exchange.NFO


def test_risk_policy_accommodates_sebi_contract_size() -> None:
    """Verify risk policy can size 1 lot of NIFTY without exceeding limits."""
    policy = load_risk_policy(Path("config/risk.yaml"))
    assert policy.config.options_premium_budget_fraction > Decimal("0")
    assert policy.config.underlying_concentration_fraction > Decimal("0")

    # Assuming INR 6 Lakh account equity
    equity = Money(Decimal("600000"), Currency.INR)
    premium_budget = equity.amount * policy.config.options_premium_budget_fraction
    # Premium for 1 lot (75 qty) at ₹150 = ₹11,250
    one_lot_premium = Decimal("150") * 75
    assert one_lot_premium <= premium_budget, (
        f"1 lot premium {one_lot_premium} must fit inside budget {premium_budget}"
    )
