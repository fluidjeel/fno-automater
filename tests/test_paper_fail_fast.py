"""Paper fail-fast identification: name a family instead of abstaining."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from trading.domain.contracts import CandidateBinding, MarketState, SetupFeatures
from trading.domain.contracts.identification import (
    MacroStatus,
    PaperTenor,
    StructureKind,
    TrendState,
    VolatilityState,
)
from trading.domain.enums import DataQuality, ReasonCode
from trading.identification import (
    BoundCandidates,
    allowed_families_for,
    load_identification_policy,
    paper_fallback_families,
    route_nifty_options,
    run_paper_scenario_matrix,
)

ROOT = Path(__file__).parents[1]
POLICY = load_identification_policy(ROOT / "config" / "identification.yaml")
STRICT = POLICY.model_copy(
    update={"router": POLICY.router.model_copy(update={"paper_fail_fast": False})}
)
NOW = datetime(2026, 9, 19, 10, 0, tzinfo=UTC)
CONTINUOUS = datetime(2026, 9, 19, 5, 0, tzinfo=UTC)


def _market(**overrides: object) -> MarketState:
    payload: dict[str, object] = {
        "market_state_id": "market-1",
        "feature_version": POLICY.feature_version,
        "calculated_at": NOW,
        "source_snapshot_ids": ("snapshot-1",),
        "trend": TrendState.UP,
        "volatility": VolatilityState.NORMAL,
        "iv_percentile": Decimal("30"),
        "iv_rv_ratio": Decimal("1.0"),
        "trend_score": Decimal("0.7"),
        "event_state": "NORMAL",
        "macro_status": MacroStatus.MISSING,
        "quality": DataQuality.VALID,
        "warmup_complete": True,
        "completed_bar_count": 60,
        "session_count": 20,
    }
    payload.update(overrides)
    return MarketState.model_validate(payload)


def _bound(strategy_id: str, score: str, *, dte: int = 5) -> BoundCandidates:
    structure = (
        StructureKind.LONG_OPTION
        if strategy_id == "positional_long_option"
        else StructureKind.DEBIT_SPREAD
    )
    setup = SetupFeatures(
        identification_rule_version=POLICY.policy_version,
        router_version=POLICY.router_version,
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
            binding_version=POLICY.binding_version,
            selected_symbols=(strategy_id,),
            score=Decimal(score),
            eligible=True,
        ),
        candidates=(),
        setup_features=setup,
    )


def _ineligible(strategy_id: str) -> BoundCandidates:
    return BoundCandidates(
        binding=CandidateBinding(
            strategy_id=strategy_id,
            binding_version=POLICY.binding_version,
            selected_symbols=(),
            score=Decimal(0),
            eligible=False,
            reason_codes=(ReasonCode.DATA_GAP,),
        ),
        candidates=(),
        setup_features=None,
    )


def test_paper_fail_fast_tie_picks_preferred_highest_score() -> None:
    """Tie no longer abstains: preferred family wins and the gap is recorded."""
    route, _ = route_nifty_options(
        _market(calculated_at=CONTINUOUS),
        long_option=_bound("positional_long_option", "0.90"),
        debit_spread=_bound("debit_spread", "0.90"),
        policy=POLICY,
    )
    assert route.paper_winner == "positional_long_option"
    assert route.forced_choice is True
    assert "min_score_gap" in route.failed_gate_ids
    assert route.paper_tenor is PaperTenor.WEEKLY


def test_paper_fail_fast_monthly_dte_is_positional_tenor() -> None:
    route, _ = route_nifty_options(
        _market(calculated_at=CONTINUOUS),
        long_option=_bound("positional_long_option", "0.90", dte=28),
        debit_spread=_bound("debit_spread", "0.75", dte=28),
        policy=POLICY,
    )
    assert route.paper_winner == "positional_long_option"
    assert route.paper_tenor is PaperTenor.POSITIONAL
    assert route.forced_choice is False


def test_paper_fail_fast_range_and_mixed_pick_cas_or_multileg() -> None:
    """RANGE/MIXED map to CAS in auction and defined-risk otherwise."""
    range_cont, _ = route_nifty_options(
        _market(
            trend=TrendState.RANGE,
            iv_percentile=Decimal("50"),
            calculated_at=CONTINUOUS,
        ),
        long_option=_ineligible("positional_long_option"),
        debit_spread=_ineligible("debit_spread"),
        policy=POLICY,
    )
    mixed_auction, _ = route_nifty_options(
        _market(trend=TrendState.MIXED, iv_percentile=Decimal("80")),
        long_option=_ineligible("positional_long_option"),
        debit_spread=_ineligible("debit_spread"),
        policy=POLICY,
    )
    assert range_cont.paper_winner == "defined_risk_multileg"
    assert mixed_auction.paper_winner == "cas_microstructure"
    assert mixed_auction.paper_tenor is PaperTenor.WEEKLY


def test_paper_fail_fast_cooldown_and_correlation_still_decide() -> None:
    """Cooldown and correlation stay visible but do not erase the winner."""
    cooldown, _ = route_nifty_options(
        _market(calculated_at=CONTINUOUS),
        long_option=_bound("positional_long_option", "0.90", dte=28),
        debit_spread=_bound("debit_spread", "0.75", dte=28),
        policy=POLICY,
        cooldown_active=True,
    )
    correlated, _ = route_nifty_options(
        _market(calculated_at=CONTINUOUS),
        long_option=_bound("positional_long_option", "0.90", dte=28),
        debit_spread=_bound("debit_spread", "0.75", dte=28),
        policy=POLICY,
        existing_correlated_exposure=True,
    )
    assert cooldown.paper_winner == "positional_long_option"
    assert cooldown.reason_codes == (ReasonCode.SETUP_COOLDOWN,)
    assert correlated.paper_winner == "positional_long_option"
    assert correlated.reason_codes == (ReasonCode.CORRELATION_LIMIT,)
    assert cooldown.forced_choice is True
    assert correlated.forced_choice is True


def test_paper_fail_fast_conflict_block_new_and_missing_iv_still_pick() -> None:
    long_option = _bound("positional_long_option", "0.90", dte=28)
    spread = _bound("debit_spread", "0.75", dte=28)
    conflict, _ = route_nifty_options(
        _market(macro_status=MacroStatus.CONFLICT, calculated_at=CONTINUOUS),
        long_option=long_option,
        debit_spread=spread,
        policy=POLICY,
    )
    blocked, _ = route_nifty_options(
        _market(event_state="BLOCK_NEW", calculated_at=CONTINUOUS),
        long_option=long_option,
        debit_spread=spread,
        policy=POLICY,
    )
    missing_iv, _ = route_nifty_options(
        _market(
            iv_percentile=None,
            iv_rv_ratio=None,
            calculated_at=CONTINUOUS,
        ),
        long_option=long_option,
        debit_spread=spread,
        policy=POLICY,
    )
    assert conflict.paper_winner == "positional_long_option"
    assert blocked.paper_winner == "positional_long_option"
    assert missing_iv.paper_winner == "positional_long_option"
    assert ReasonCode.EVENT_BLACKOUT in conflict.reason_codes
    assert ReasonCode.EVENT_BLACKOUT in blocked.reason_codes
    assert ReasonCode.WARMUP_INCOMPLETE in missing_iv.reason_codes


def test_live_strict_still_abstains_when_flag_off() -> None:
    route, _ = route_nifty_options(
        _market(event_state="BLOCK_NEW", calculated_at=CONTINUOUS),
        long_option=_bound("positional_long_option", "0.90"),
        debit_spread=_bound("debit_spread", "0.75"),
        policy=STRICT,
    )
    assert route.paper_winner is None
    assert allowed_families_for(_market(event_state="BLOCK_NEW"), STRICT) == frozenset()


def test_paper_fallback_keeps_cas_auction_only() -> None:
    auction = paper_fallback_families(_market(trend=TrendState.MIXED), POLICY)
    continuous = paper_fallback_families(
        _market(trend=TrendState.MIXED, calculated_at=CONTINUOUS), POLICY
    )
    assert "cas_microstructure" in auction
    assert "cas_microstructure" not in continuous
    assert "defined_risk_multileg" in continuous
    assert "commodity_futures_trend" not in auction


def test_paper_scenario_matrix_names_a_winner_on_every_cell() -> None:
    """The 15-cell harness must not hide behind router_abstain."""
    rows = run_paper_scenario_matrix(POLICY)
    assert len(rows) == 15
    missing = [row.scenario_id for row in rows if row.paper_winner is None]
    assert missing == []
    winners = {row.paper_winner for row in rows}
    assert "cas_microstructure" in winners
    assert "positional_long_option" in winners
    assert "debit_spread" in winners
    assert "defined_risk_multileg" in winners
    tenors = {row.paper_tenor for row in rows if row.paper_tenor is not None}
    assert PaperTenor.WEEKLY in tenors
    assert PaperTenor.POSITIONAL in tenors
