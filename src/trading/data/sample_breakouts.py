"""Synthetic market data generator for testing breakout profitability.

Generates realistic, randomized market data snapshots with genuine breakouts and
fake breakouts (bull/bear traps, spoofed auction imbalances, liquidity vacuums)
across Positional, Directional, and Close-Auction Microstructure (CAS) scenarios.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from enum import StrEnum, unique

from trading.domain.contracts.advice import StructureChoice
from trading.domain.contracts.common import (
    ContractRef,
    DataQualityReport,
    Lineage,
    Versions,
)
from trading.domain.contracts.entry import LegSpec, StrikeCandidate, StrikeShortlist
from trading.domain.contracts.portfolio import PortfolioView
from trading.domain.contracts.snapshot import (
    DerivativesContext,
    FeatureSnapshot,
    MarketQuote,
    SnapshotTimes,
)
from trading.domain.enums import (
    AssetClass,
    DataQuality,
    Exchange,
    InstrumentKind,
    LiquidityGrade,
    OptionType,
    Side,
    SystemState,
)
from trading.domain.primitives import Currency, Money, Percent, Price, TickSize
from trading.strategies.cas_microstructure import (
    FEATURE_AUCTION_IMBALANCE,
    FEATURE_MICROPRICE_EDGE_BPS,
    FEATURE_QUOTE_INSTABILITY,
    FEATURE_SET_VERSION,
    FEATURE_TRADE_FLOW_IMBALANCE,
)
from trading.strategies.macro import MacroAssessment, MacroBias

__all__ = [
    "BreakoutMarketCase",
    "BreakoutScenarioKind",
    "BreakoutType",
    "generate_breakout_cases",
]

INR = Currency.INR
IST = timezone(timedelta(hours=5, minutes=30))
_TICK = TickSize.of("0.05")


@unique
class BreakoutType(StrEnum):
    """Classification of market move at the decision boundary."""

    GENUINE_BULL = "GENUINE_BULL"
    GENUINE_BEAR = "GENUINE_BEAR"
    FAKE_BULL_TRAP = "FAKE_BULL_TRAP"
    FAKE_BEAR_TRAP = "FAKE_BEAR_TRAP"


@unique
class BreakoutScenarioKind(StrEnum):
    """Trading strategy scenario category."""

    POSITIONAL = "POSITIONAL"
    DIRECTIONAL = "DIRECTIONAL"
    CAS = "CAS"


@dataclass(frozen=True, slots=True)
class BreakoutMarketCase:
    """A self-contained synthetic market state evaluated by both layers."""

    case_id: str
    scenario_kind: BreakoutScenarioKind
    breakout_type: BreakoutType
    underlying_snapshot: FeatureSnapshot
    candidate_snapshots: tuple[FeatureSnapshot, ...]
    portfolio_view: PortfolioView
    as_of: datetime
    macro: MacroAssessment | None
    shortlist: StrikeShortlist
    is_fake: bool
    baseline_outcome_r: Decimal  # Full 1.0x baseline return in R (e.g. +1.8R or -1.0R)
    expected_bias: MacroBias
    invalidation_narrative: str


def _price(val: str | Decimal) -> Price:
    return Price.snap(Decimal(str(val)), _TICK)


def _money(val: str | Decimal) -> Money:
    return Money.of(Decimal(str(val)), INR)


def _versions() -> Versions:
    return Versions(
        code_version="0.1.0",
        config_version="1",
        config_checksum="synthbreakout01",
    )


def _lineage() -> Lineage:
    return Lineage(
        provider="synthetic_breakout_generator",
        source_ids=("synth-feed-1",),
        raw_event_refs=("ref-001",),
        normalization_version="1",
        versions=_versions(),
    )


def _quality() -> DataQualityReport:
    return DataQualityReport(
        state=DataQuality.VALID,
        reason_codes=(),
        source_status="connected",
        warmup_complete=True,
        age_ms=80,
    )


def _snapshot_times(as_of: datetime) -> SnapshotTimes:
    calc = as_of - timedelta(seconds=2)
    return SnapshotTimes(
        event_time=calc,
        source_time=calc,
        receive_time=calc + timedelta(milliseconds=40),
        calculation_time=calc + timedelta(milliseconds=90),
    )


def _portfolio_view(as_of: datetime) -> PortfolioView:
    return PortfolioView.model_validate(
        {
            "as_of": as_of,
            "open_trade_count": 0,
            "margin_available": _money("1000000"),
            "realized_pnl_today": _money("0"),
            "net_delta": 0,
            "system_state": SystemState.READY,
            "entries_permitted": True,
        }
    )


def _index_contract(symbol: str = "NIFTY") -> ContractRef:
    return ContractRef.model_validate(
        {
            "exchange": Exchange.NSE,
            "symbol": symbol,
            "instrument_kind": InstrumentKind.INDEX,
            "asset_class": AssetClass.EQUITY_INDEX,
            "underlying": symbol,
            "expiry": None,
            "strike": None,
            "option_type": None,
        }
    )


def _option_contract(
    symbol: str,
    strike: Decimal,
    option_type: OptionType,
    expiry: date,
    underlying: str = "NIFTY",
) -> ContractRef:
    return ContractRef.model_validate(
        {
            "exchange": Exchange.NFO,
            "symbol": symbol,
            "instrument_kind": InstrumentKind.OPTION,
            "asset_class": AssetClass.EQUITY_INDEX,
            "underlying": underlying,
            "expiry": expiry,
            "strike": strike,
            "option_type": option_type,
        }
    )


def _macro_assessment(
    as_of: datetime, bias: MacroBias, confidence: Decimal
) -> MacroAssessment:
    return MacroAssessment(
        regime="TREND_EXPANSION" if bias is not MacroBias.NEUTRAL else "RANGE",
        directional_bias=bias,
        confidence=confidence,
        fresh_until=as_of + timedelta(hours=4),
        evidence_ids=("macro-feed-1",),
        model_version="macro-synth-v1",
    )


def _build_shortlist(
    snapshot_id: str,
    as_of: datetime,
    expiry: date,
    structure: StructureChoice,
    candidates: tuple[StrikeCandidate, ...],
) -> StrikeShortlist:
    return StrikeShortlist(
        snapshot_id=snapshot_id,
        underlying="NIFTY",
        expiry=expiry,
        structure=structure,
        candidates=candidates,
        shortlist_rule_version="shortlist-v1",
    )


def generate_breakout_cases(
    scenario_kind: BreakoutScenarioKind,
    count: int = 35,
    seed: int = 42,
) -> list[BreakoutMarketCase]:
    """Generate deterministic randomized market cases for a given scenario.

    Ensures at least 45% fake breakouts and 45% genuine breakouts to rigorously
    test how deterministic vs agentic layers handle traps.
    """
    rng = random.Random(seed + hash(scenario_kind.value))  # noqa: S311
    cases: list[BreakoutMarketCase] = []

    # Monday 2026-09-14 10:00 UTC (15:30 IST) baseline
    base_date = datetime(2026, 9, 14, tzinfo=UTC)

    for i in range(count):
        case_id = f"{scenario_kind.value}-{i + 1:03d}"
        # Alternate and randomize genuine vs fake breakouts
        is_fake = (i % 2 == 1) if i < count - 3 else rng.choice([True, False])
        is_bull = rng.choice([True, False])

        if is_bull:
            breakout_type = (
                BreakoutType.FAKE_BULL_TRAP if is_fake else BreakoutType.GENUINE_BULL
            )
        else:
            breakout_type = (
                BreakoutType.FAKE_BEAR_TRAP if is_fake else BreakoutType.GENUINE_BEAR
            )

        if scenario_kind is BreakoutScenarioKind.POSITIONAL:
            case = _build_positional_case(rng, case_id, base_date, i, breakout_type)
        elif scenario_kind is BreakoutScenarioKind.DIRECTIONAL:
            case = _build_directional_case(rng, case_id, base_date, i, breakout_type)
        else:
            case = _build_cas_case(rng, case_id, base_date, i, breakout_type)

        cases.append(case)

    return cases


def _build_positional_case(
    rng: random.Random,
    case_id: str,
    base_date: datetime,
    index: int,
    breakout_type: BreakoutType,
) -> BreakoutMarketCase:
    """Positional debit spread scenario: DTE 14-28, multi-day defined-risk."""
    as_of = base_date + timedelta(days=index, hours=4)  # 09:30 IST
    expiry = (as_of + timedelta(days=14)).date()
    is_fake = breakout_type in (
        BreakoutType.FAKE_BULL_TRAP,
        BreakoutType.FAKE_BEAR_TRAP,
    )
    is_bull = breakout_type in (
        BreakoutType.GENUINE_BULL,
        BreakoutType.FAKE_BULL_TRAP,
    )
    opt_type = OptionType.CALL if is_bull else OptionType.PUT

    base_spot = Decimal(24000 + (index * 50) % 800)
    close_val = base_spot
    move_pct = Decimal(str(rng.uniform(0.0025, 0.0065)))
    last_val = (
        close_val * (Decimal("1.0") + move_pct)
        if is_bull
        else close_val * (Decimal("1.0") - move_pct)
    )

    underlying_snap = FeatureSnapshot(
        snapshot_id=f"SNAP-UND-{case_id}",
        contract=_index_contract("NIFTY"),
        times=_snapshot_times(as_of),
        market=MarketQuote(
            bid=_price(last_val - Decimal("1.0")),
            ask=_price(last_val + Decimal("1.0")),
            last=_price(last_val),
            close=_price(close_val),
        ),
        feature_set_version="1",
        features={"realized_vol_20d": Decimal("0.14")},
        quality=_quality(),
        lineage=_lineage(),
    )

    # Debit spread legs: Long strike (ATM) and Short strike (OTM)
    strike_long = base_spot
    spread_offset = Decimal("200") if is_bull else Decimal("-200")
    strike_short = base_spot + spread_offset

    exp_str = expiry.strftime("%d%b").upper()
    long_sym = f"NIFTY{exp_str}{int(strike_long)}{opt_type.value}"
    short_sym = f"NIFTY{exp_str}{int(strike_short)}{opt_type.value}"

    # Genuine breakouts have tight spreads and high OI; fake breakouts have fragility
    long_spread = Decimal("0.015") if not is_fake else Decimal("0.038")
    short_spread = Decimal("0.020") if not is_fake else Decimal("0.042")
    oi_long = 8000 if not is_fake else 1500
    oi_short = 6500 if not is_fake else 1200

    long_mid = Decimal("160.00")
    short_mid = Decimal("70.00")

    long_snap = FeatureSnapshot(
        snapshot_id=f"SNAP-OPT1-{case_id}",
        contract=_option_contract(long_sym, strike_long, opt_type, expiry),
        times=_snapshot_times(as_of),
        market=MarketQuote(
            bid=_price(long_mid * (Decimal("1.0") - long_spread / 2)),
            ask=_price(long_mid * (Decimal("1.0") + long_spread / 2)),
            last=_price(long_mid),
        ),
        feature_set_version="1",
        features={"iv": Decimal("0.15")},
        quality=_quality(),
        lineage=_lineage(),
        derivatives=DerivativesContext(
            days_to_expiry=14,
            open_interest=oi_long,
            option_type=opt_type,
            underlying_price=_price(last_val),
        ),
    )

    short_snap = FeatureSnapshot(
        snapshot_id=f"SNAP-OPT2-{case_id}",
        contract=_option_contract(short_sym, strike_short, opt_type, expiry),
        times=_snapshot_times(as_of),
        market=MarketQuote(
            bid=_price(short_mid * (Decimal("1.0") - short_spread / 2)),
            ask=_price(short_mid * (Decimal("1.0") + short_spread / 2)),
            last=_price(short_mid),
        ),
        feature_set_version="1",
        features={"iv": Decimal("0.16")},
        quality=_quality(),
        lineage=_lineage(),
        derivatives=DerivativesContext(
            days_to_expiry=14,
            open_interest=oi_short,
            option_type=opt_type,
            underlying_price=_price(last_val),
        ),
    )

    # Construct shortlist candidates for Agent Desk
    net_debit = _money("6750")  # (160 - 70) * 75
    max_loss = net_debit
    c1_score = Decimal("0.92") if not is_fake else Decimal("0.35")
    c2_score = Decimal("0.75") if not is_fake else Decimal("0.25")
    grade = LiquidityGrade.A if not is_fake else LiquidityGrade.C

    candidate1 = StrikeCandidate(
        candidate_id=f"CAND-1-{case_id}",
        legs=(
            LegSpec(
                leg_id="leg-long",
                contract=long_snap.contract,
                side=Side.BUY,
                ratio=1,
            ),
            LegSpec(
                leg_id="leg-short",
                contract=short_snap.contract,
                side=Side.SELL,
                ratio=1,
            ),
        ),
        net_debit=net_debit,
        max_loss=max_loss,
        deterministic_score=c1_score,
        liquidity_grade=grade,
        bid_ask_spread_pct=Percent.from_fraction(long_spread),
        breakeven_move_pct=Percent.from_percent("0.8"),
    )
    candidate2 = StrikeCandidate(
        candidate_id=f"CAND-2-{case_id}",
        legs=(
            LegSpec(
                leg_id="leg-long-alt",
                contract=long_snap.contract,
                side=Side.BUY,
                ratio=1,
            ),
            LegSpec(
                leg_id="leg-short-alt",
                contract=short_snap.contract,
                side=Side.SELL,
                ratio=1,
            ),
        ),
        net_debit=net_debit,
        max_loss=max_loss,
        deterministic_score=c2_score,
        liquidity_grade=grade,
        bid_ask_spread_pct=Percent.from_fraction(short_spread),
        breakeven_move_pct=Percent.from_percent("1.2"),
    )

    shortlist = _build_shortlist(
        snapshot_id=underlying_snap.snapshot_id,
        as_of=as_of,
        expiry=expiry,
        structure=StructureChoice.DEBIT_SPREAD,
        candidates=(candidate1, candidate2),
    )

    macro = _macro_assessment(
        as_of,
        MacroBias.BULLISH if is_bull else MacroBias.BEARISH,
        Decimal("0.85") if not is_fake else Decimal("0.35"),
    )

    baseline_r = (
        Decimal("1.80") if not is_fake else Decimal("-1.00")
    )  # Win +1.8R or Loss -1.0R
    invalidation = (
        "Genuine positional trend continuation"
        if not is_fake
        else "Fake breakout: liquidity exhaustion at multi-day resistance"
    )

    return BreakoutMarketCase(
        case_id=case_id,
        scenario_kind=BreakoutScenarioKind.POSITIONAL,
        breakout_type=breakout_type,
        underlying_snapshot=underlying_snap,
        candidate_snapshots=(long_snap, short_snap),
        portfolio_view=_portfolio_view(as_of),
        as_of=as_of,
        macro=macro,
        shortlist=shortlist,
        is_fake=is_fake,
        baseline_outcome_r=baseline_r,
        expected_bias=MacroBias.BULLISH if is_bull else MacroBias.BEARISH,
        invalidation_narrative=invalidation,
    )


def _build_directional_case(
    rng: random.Random,
    case_id: str,
    base_date: datetime,
    index: int,
    breakout_type: BreakoutType,
) -> BreakoutMarketCase:
    """Directional outright option scenario: DTE 7-10, fast momentum."""
    as_of = base_date + timedelta(days=index, hours=5, minutes=15)  # 10:45 IST
    expiry = (as_of + timedelta(days=7)).date()
    is_fake = breakout_type in (
        BreakoutType.FAKE_BULL_TRAP,
        BreakoutType.FAKE_BEAR_TRAP,
    )
    is_bull = breakout_type in (
        BreakoutType.GENUINE_BULL,
        BreakoutType.FAKE_BULL_TRAP,
    )
    opt_type = OptionType.CALL if is_bull else OptionType.PUT

    base_spot = Decimal(24100 + (index * 40) % 700)
    close_val = base_spot
    move_pct = Decimal(str(rng.uniform(0.0018, 0.0045)))
    last_val = (
        close_val * (Decimal("1.0") + move_pct)
        if is_bull
        else close_val * (Decimal("1.0") - move_pct)
    )

    underlying_snap = FeatureSnapshot(
        snapshot_id=f"SNAP-UND-{case_id}",
        contract=_index_contract("NIFTY"),
        times=_snapshot_times(as_of),
        market=MarketQuote(
            bid=_price(last_val - Decimal("1.0")),
            ask=_price(last_val + Decimal("1.0")),
            last=_price(last_val),
            close=_price(close_val),
        ),
        feature_set_version="1",
        features={"realized_vol_20d": Decimal("0.13")},
        quality=_quality(),
        lineage=_lineage(),
    )

    strike = base_spot
    sym = f"NIFTY{expiry.strftime('%d%b').upper()}{int(strike)}{opt_type.value}"
    spread_pct = Decimal("0.012") if not is_fake else Decimal("0.035")
    oi = 6000 if not is_fake else 1200
    mid = Decimal("135.00")

    opt_snap = FeatureSnapshot(
        snapshot_id=f"SNAP-OPT-{case_id}",
        contract=_option_contract(sym, strike, opt_type, expiry),
        times=_snapshot_times(as_of),
        market=MarketQuote(
            bid=_price(mid * (Decimal("1.0") - spread_pct / 2)),
            ask=_price(mid * (Decimal("1.0") + spread_pct / 2)),
            last=_price(mid),
        ),
        feature_set_version="1",
        features={"iv": Decimal("0.15")},
        quality=_quality(),
        lineage=_lineage(),
        derivatives=DerivativesContext(
            days_to_expiry=7,
            open_interest=oi,
            option_type=opt_type,
            underlying_price=_price(last_val),
        ),
    )

    # Shortlist candidates for Agent Desk
    net_debit = _money("10125")  # 135 * 75
    c1_score = Decimal("0.89") if not is_fake else Decimal("0.38")
    c2_score = Decimal("0.72") if not is_fake else Decimal("0.28")
    grade = LiquidityGrade.A if not is_fake else LiquidityGrade.B

    candidate1 = StrikeCandidate(
        candidate_id=f"CAND-1-{case_id}",
        legs=(
            LegSpec(
                leg_id="leg-opt",
                contract=opt_snap.contract,
                side=Side.BUY,
                ratio=1,
            ),
        ),
        net_debit=net_debit,
        max_loss=net_debit,
        deterministic_score=c1_score,
        liquidity_grade=grade,
        bid_ask_spread_pct=Percent.from_fraction(spread_pct),
        breakeven_move_pct=Percent.from_percent("0.5"),
    )
    candidate2 = StrikeCandidate(
        candidate_id=f"CAND-2-{case_id}",
        legs=(
            LegSpec(
                leg_id="leg-opt-alt",
                contract=opt_snap.contract,
                side=Side.BUY,
                ratio=1,
            ),
        ),
        net_debit=net_debit,
        max_loss=net_debit,
        deterministic_score=c2_score,
        liquidity_grade=grade,
        bid_ask_spread_pct=Percent.from_fraction(spread_pct * Decimal("1.2")),
        breakeven_move_pct=Percent.from_percent("0.7"),
    )

    shortlist = _build_shortlist(
        snapshot_id=underlying_snap.snapshot_id,
        as_of=as_of,
        expiry=expiry,
        structure=StructureChoice.POSITIONAL_LONG_OPTION,
        candidates=(candidate1, candidate2),
    )

    macro = _macro_assessment(
        as_of,
        MacroBias.BULLISH if is_bull else MacroBias.BEARISH,
        Decimal("0.80") if not is_fake else Decimal("0.40"),
    )

    baseline_r = Decimal("2.00") if not is_fake else Decimal("-1.00")
    invalidation = (
        "Genuine directional momentum expansion"
        if not is_fake
        else "Fake breakout: upper wick rejection with tape divergence"
    )

    return BreakoutMarketCase(
        case_id=case_id,
        scenario_kind=BreakoutScenarioKind.DIRECTIONAL,
        breakout_type=breakout_type,
        underlying_snapshot=underlying_snap,
        candidate_snapshots=(opt_snap,),
        portfolio_view=_portfolio_view(as_of),
        as_of=as_of,
        macro=macro,
        shortlist=shortlist,
        is_fake=is_fake,
        baseline_outcome_r=baseline_r,
        expected_bias=MacroBias.BULLISH if is_bull else MacroBias.BEARISH,
        invalidation_narrative=invalidation,
    )


def _build_cas_case(
    rng: random.Random,
    case_id: str,
    base_date: datetime,
    index: int,
    breakout_type: BreakoutType,
) -> BreakoutMarketCase:
    """Close-Auction Microstructure (CAS) scenario: 15:00-15:30 IST window."""
    # 09:45 UTC = 15:15 IST (within CAS 15:00-15:30 window on a weekday)
    day_offset = index % 5
    as_of = (
        base_date.replace(hour=9, minute=45, second=0) + timedelta(days=day_offset)
    )
    expiry = (as_of + timedelta(days=3)).date()
    is_fake = breakout_type in (
        BreakoutType.FAKE_BULL_TRAP,
        BreakoutType.FAKE_BEAR_TRAP,
    )
    is_bull = breakout_type in (
        BreakoutType.GENUINE_BULL,
        BreakoutType.FAKE_BULL_TRAP,
    )
    opt_type = OptionType.CALL if is_bull else OptionType.PUT

    base_spot = Decimal(24200 + (index * 30) % 500)
    close_val = base_spot
    move_pct = Decimal(str(rng.uniform(0.0015, 0.0035)))
    last_val = (
        close_val * (Decimal("1.0") + move_pct)
        if is_bull
        else close_val * (Decimal("1.0") - move_pct)
    )

    # CAS feature contract keys:
    # Deterministic strategy enters if:
    # 1. net pressure >= +0.10 (bullish) or <= -0.10 (bearish)
    # 2. microprice_edge >= 0 (bullish) or <= 0 (bearish)
    # 3. quote_instability <= 0.50
    #
    # Genuine CAS: High aligned imbalance, low quote instability (< 0.20)
    # Fake CAS: Spoofed book! Net pressure meets bare minimum (+0.12), but trade flow
    # opposes auction and instability is elevated (0.45).
    if not is_fake:
        auc_imb = Decimal("0.40") if is_bull else Decimal("-0.40")
        flow_imb = Decimal("0.30") if is_bull else Decimal("-0.30")
        edge_bps = Decimal("6.00") if is_bull else Decimal("-6.00")
        instability = Decimal("0.12")
    else:
        auc_imb = Decimal("0.35") if is_bull else Decimal("-0.35")
        flow_imb = Decimal("-0.23") if is_bull else Decimal("0.23")
        edge_bps = Decimal("0.50") if is_bull else Decimal("-0.50")
        instability = Decimal("0.45")

    cas_features: dict[str, Decimal] = {
        FEATURE_AUCTION_IMBALANCE: auc_imb,
        FEATURE_TRADE_FLOW_IMBALANCE: flow_imb,
        FEATURE_MICROPRICE_EDGE_BPS: edge_bps,
        FEATURE_QUOTE_INSTABILITY: instability,
    }

    underlying_snap = FeatureSnapshot(
        snapshot_id=f"SNAP-UND-{case_id}",
        contract=_index_contract("NIFTY"),
        times=_snapshot_times(as_of),
        market=MarketQuote(
            bid=_price(last_val - Decimal("0.5")),
            ask=_price(last_val + Decimal("0.5")),
            last=_price(last_val),
            close=_price(close_val),
        ),
        feature_set_version=FEATURE_SET_VERSION,
        features=cas_features,
        quality=_quality(),
        lineage=_lineage(),
    )

    strike = base_spot
    sym = f"NIFTY{expiry.strftime('%d%b').upper()}{int(strike)}{opt_type.value}"
    mid = Decimal("95.00")
    spread_pct = Decimal("0.015") if not is_fake else Decimal("0.035")
    oi = 4000 if not is_fake else 800

    opt_snap = FeatureSnapshot(
        snapshot_id=f"SNAP-OPT-{case_id}",
        contract=_option_contract(sym, strike, opt_type, expiry),
        times=_snapshot_times(as_of),
        market=MarketQuote(
            bid=_price(mid * (Decimal("1.0") - spread_pct / 2)),
            ask=_price(mid * (Decimal("1.0") + spread_pct / 2)),
            last=_price(mid),
        ),
        feature_set_version="1",
        features={"iv": Decimal("0.14")},
        quality=_quality(),
        lineage=_lineage(),
        derivatives=DerivativesContext(
            days_to_expiry=3,
            open_interest=oi,
            option_type=opt_type,
            underlying_price=_price(last_val),
        ),
    )

    net_debit = _money("7125")  # 95 * 75
    c1_score = Decimal("0.91") if not is_fake else Decimal("0.32")
    c2_score = Decimal("0.70") if not is_fake else Decimal("0.22")
    grade = LiquidityGrade.A if not is_fake else LiquidityGrade.C

    candidate1 = StrikeCandidate(
        candidate_id=f"CAND-1-{case_id}",
        legs=(
            LegSpec(
                leg_id="leg-opt",
                contract=opt_snap.contract,
                side=Side.BUY,
                ratio=1,
            ),
        ),
        net_debit=net_debit,
        max_loss=net_debit,
        deterministic_score=c1_score,
        liquidity_grade=grade,
        bid_ask_spread_pct=Percent.from_fraction(spread_pct),
        breakeven_move_pct=Percent.from_percent("0.4"),
    )
    candidate2 = StrikeCandidate(
        candidate_id=f"CAND-2-{case_id}",
        legs=(
            LegSpec(
                leg_id="leg-opt-alt",
                contract=opt_snap.contract,
                side=Side.BUY,
                ratio=1,
            ),
        ),
        net_debit=net_debit,
        max_loss=net_debit,
        deterministic_score=c2_score,
        liquidity_grade=grade,
        bid_ask_spread_pct=Percent.from_fraction(spread_pct * Decimal("1.3")),
        breakeven_move_pct=Percent.from_percent("0.6"),
    )

    shortlist = _build_shortlist(
        snapshot_id=underlying_snap.snapshot_id,
        as_of=as_of,
        expiry=expiry,
        structure=StructureChoice.CAS_MICROSTRUCTURE,
        candidates=(candidate1, candidate2),
    )

    macro = _macro_assessment(
        as_of,
        MacroBias.BULLISH if is_bull else MacroBias.BEARISH,
        Decimal("0.85") if not is_fake else Decimal("0.30"),
    )

    baseline_r = Decimal("1.50") if not is_fake else Decimal("-1.00")
    invalidation = (
        "Genuine auction volume imbalance with sustained closing print"
        if not is_fake
        else "Fake auction spoof: severe order flow divergence and high instability"
    )

    return BreakoutMarketCase(
        case_id=case_id,
        scenario_kind=BreakoutScenarioKind.CAS,
        breakout_type=breakout_type,
        underlying_snapshot=underlying_snap,
        candidate_snapshots=(opt_snap,),
        portfolio_view=_portfolio_view(as_of),
        as_of=as_of,
        macro=macro,
        shortlist=shortlist,
        is_fake=is_fake,
        baseline_outcome_r=baseline_r,
        expected_bias=MacroBias.BULLISH if is_bull else MacroBias.BEARISH,
        invalidation_narrative=invalidation,
    )
