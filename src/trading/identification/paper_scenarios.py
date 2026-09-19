"""Offline 15-cell paper routing matrix. No network, no broker."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from trading.domain.contracts import CandidateBinding, MarketState, SetupFeatures
from trading.domain.contracts.identification import (
    MacroStatus,
    PaperTenor,
    StructureKind,
    TrendState,
    VolatilityState,
)
from trading.domain.enums import DataQuality, ReasonCode
from trading.identification.allow_table import session_bucket_for
from trading.identification.binders import BoundCandidates
from trading.identification.config import IdentificationPolicy
from trading.identification.router import route_nifty_options

__all__ = ["ScenarioRow", "format_matrix", "run_paper_scenario_matrix"]

AUCTION_AT = datetime(2026, 9, 19, 10, 0, tzinfo=UTC)
CONTINUOUS_AT = datetime(2026, 9, 19, 5, 0, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class ScenarioRow:
    scenario_id: str
    trend: str
    iv: str
    session: str
    gates: str
    paper_winner: str | None
    paper_tenor: PaperTenor | None
    forced_choice: bool
    failed_gate_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Spec:
    scenario_id: str
    trend: TrendState
    iv_percentile: Decimal | None
    event_state: str = "NORMAL"
    macro_status: MacroStatus = MacroStatus.MISSING
    calculated_at: datetime = CONTINUOUS_AT
    cooldown_active: bool = False
    correlated: bool = False
    warmup_complete: bool = True
    long_score: str = "0.90"
    debit_score: str = "0.75"
    long_dte: int = 28
    directional_eligible: bool = True
    gates: str = ""


def run_paper_scenario_matrix(
    policy: IdentificationPolicy,
) -> tuple[ScenarioRow, ...]:
    """Route the 15 paper identification cells against ``policy``."""
    return tuple(_run_one(spec, policy) for spec in _SPECS)


def format_matrix(rows: tuple[ScenarioRow, ...] | list[ScenarioRow]) -> str:
    """Render a fixed-width table for the CLI harness."""
    header = (
        f"{'id':<28} {'trend':<7} {'iv':<6} {'session':<11} "
        f"{'winner':<24} {'tenor':<11} {'forced':<7} failed_gates"
    )
    lines = [header, "-" * len(header)]
    for row in rows:
        winner = row.paper_winner or "None"
        tenor = "-" if row.paper_tenor is None else row.paper_tenor.value
        gates = ",".join(row.failed_gate_ids) if row.failed_gate_ids else "-"
        lines.append(
            f"{row.scenario_id:<28} {row.trend:<7} {row.iv:<6} {row.session:<11} "
            f"{winner:<24} {tenor:<11} {row.forced_choice!s:<7} {gates}"
        )
    winners = sum(1 for row in rows if row.paper_winner is not None)
    abstain = len(rows) - winners
    forced = sum(1 for row in rows if row.forced_choice)
    lines.append("")
    lines.append(
        f"summary: routed={winners} router_abstain={abstain} "
        f"forced_choice={forced} n={len(rows)}"
    )
    return "\n".join(lines)


def _run_one(spec: _Spec, policy: IdentificationPolicy) -> ScenarioRow:
    market = _market(spec, policy)
    long_option = _bound(
        "positional_long_option",
        spec.long_score,
        policy,
        dte=spec.long_dte,
        eligible=spec.directional_eligible,
    )
    debit = _bound(
        "debit_spread",
        spec.debit_score,
        policy,
        dte=spec.long_dte,
        eligible=spec.directional_eligible,
    )
    decision, _opportunities = route_nifty_options(
        market,
        long_option=long_option,
        debit_spread=debit,
        policy=policy,
        cooldown_active=spec.cooldown_active,
        existing_correlated_exposure=spec.correlated,
    )
    iv = "-" if spec.iv_percentile is None else str(spec.iv_percentile)
    return ScenarioRow(
        scenario_id=spec.scenario_id,
        trend=spec.trend.value,
        iv=iv,
        session=session_bucket_for(spec.calculated_at, policy).value,
        gates=spec.gates,
        paper_winner=decision.paper_winner,
        paper_tenor=decision.paper_tenor,
        forced_choice=decision.forced_choice,
        failed_gate_ids=decision.failed_gate_ids,
    )


def _market(spec: _Spec, policy: IdentificationPolicy) -> MarketState:
    iv = spec.iv_percentile
    rv = None if iv is None else Decimal("1.0")
    if iv is not None and iv > policy.router.low_iv_percentile:
        rv = Decimal("1.3")
    return MarketState(
        market_state_id=f"scenario-{spec.scenario_id}",
        feature_version=policy.feature_version,
        calculated_at=spec.calculated_at,
        source_snapshot_ids=("snapshot-1",),
        trend=spec.trend,
        volatility=VolatilityState.NORMAL,
        iv_percentile=iv,
        iv_rv_ratio=rv,
        trend_score=Decimal("0.7"),
        event_state=spec.event_state,
        macro_status=spec.macro_status,
        quality=DataQuality.VALID,
        warmup_complete=spec.warmup_complete,
        completed_bar_count=60,
        session_count=20,
    )


def _bound(
    strategy_id: str,
    score: str,
    policy: IdentificationPolicy,
    *,
    dte: int,
    eligible: bool,
) -> BoundCandidates:
    structure = (
        StructureKind.LONG_OPTION
        if strategy_id == "positional_long_option"
        else StructureKind.DEBIT_SPREAD
    )
    setup = None
    if eligible:
        setup = SetupFeatures(
            identification_rule_version=policy.policy_version,
            router_version=policy.router_version,
            market_state_id="market-1",
            raw_setup_score=Decimal(score),
            score_components={"contract_binding": Decimal(score)},
            trend=TrendState.UP,
            volatility=VolatilityState.NORMAL,
            structure=structure,
            dte=dte,
            delta=Decimal("0.5"),
            implied_volatility=Decimal("15"),
            iv_percentile=Decimal("30"),
            iv_rv_ratio=Decimal("1"),
            open_interest=5000,
            spread_fraction=Decimal("0.01"),
            liquidity_rank=Decimal(score),
            event_state="NORMAL",
            macro_status=MacroStatus.MISSING,
        )
    return BoundCandidates(
        binding=CandidateBinding(
            strategy_id=strategy_id,
            binding_version=policy.binding_version,
            selected_symbols=(strategy_id,) if eligible else (),
            score=Decimal(score) if eligible else Decimal(0),
            eligible=eligible,
            reason_codes=() if eligible else (ReasonCode.DATA_GAP,),
        ),
        candidates=(),
        setup_features=setup,
    )


_SPECS: tuple[_Spec, ...] = (
    _Spec(
        "up_low_iv_positional",
        TrendState.UP,
        Decimal("30"),
        long_dte=28,
        gates="direction",
    ),
    _Spec(
        "up_high_iv_debit",
        TrendState.UP,
        Decimal("70"),
        debit_score="0.90",
        long_score="0.75",
        gates="direction",
    ),
    _Spec(
        "down_mid_iv_debit",
        TrendState.DOWN,
        Decimal("55"),
        debit_score="0.88",
        long_score="0.70",
        gates="direction",
    ),
    _Spec(
        "range_high_iv_multileg",
        TrendState.RANGE,
        Decimal("80"),
        directional_eligible=False,
        gates="range",
    ),
    _Spec(
        "range_mid_iv_fallback",
        TrendState.RANGE,
        Decimal("50"),
        directional_eligible=False,
        gates="range-fallback",
    ),
    _Spec(
        "mixed_auction_cas",
        TrendState.MIXED,
        Decimal("80"),
        calculated_at=AUCTION_AT,
        directional_eligible=False,
        gates="cas",
    ),
    _Spec(
        "mixed_continuous_multileg",
        TrendState.MIXED,
        Decimal("50"),
        directional_eligible=False,
        gates="mixed-fallback",
    ),
    _Spec(
        "up_auction_cas",
        TrendState.UP,
        Decimal("30"),
        calculated_at=AUCTION_AT,
        gates="cas",
    ),
    _Spec(
        "range_auction_cas",
        TrendState.RANGE,
        Decimal("80"),
        calculated_at=AUCTION_AT,
        directional_eligible=False,
        gates="cas",
    ),
    _Spec(
        "tie_highest_preferred",
        TrendState.UP,
        Decimal("30"),
        debit_score="0.90",
        long_score="0.90",
        long_dte=5,
        gates="tie",
    ),
    _Spec(
        "cooldown_still_picks",
        TrendState.UP,
        Decimal("30"),
        cooldown_active=True,
        gates="cooldown",
    ),
    _Spec(
        "correlation_still_picks",
        TrendState.UP,
        Decimal("30"),
        correlated=True,
        gates="correlation",
    ),
    _Spec(
        "missing_iv_still_picks",
        TrendState.UP,
        None,
        gates="iv-gap",
    ),
    _Spec(
        "event_block_new_picks",
        TrendState.UP,
        Decimal("30"),
        event_state="BLOCK_NEW",
        gates="event",
    ),
    _Spec(
        "macro_conflict_picks",
        TrendState.UP,
        Decimal("30"),
        macro_status=MacroStatus.CONFLICT,
        gates="macro",
    ),
)
