"""Two-tier PAPER data contract: P0 fail-closed, P1 observed-only.

Invariant 6: missing, stale or zero-invalid P0 state blocks new exposure.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

import tests.factories as f
from tests.test_identification import _bound, _market
from tests.test_paper_lifecycle import _option_snapshot
from tests.test_paper_runner import (
    BROKER_FIXTURES,
    _paper_config,
    _request,
)
from tests.test_risk_gateway import (
    ACCOUNT_CONFIG,
    RISK_POLICY,
    _gateway_request,
    instrument_spec,
    option_snapshot,
)
from trading.broker.paper import PaperBroker
from trading.config import load_paper_data_requirements, load_risk_policy
from trading.data.events import CanonicalMarketEvent
from trading.data.prices import depth_top_sizes, observed_book_sizes
from trading.domain.clock import FrozenClock
from trading.domain.contracts import FeatureSnapshot, InstrumentSpec, MarketState
from trading.domain.contracts.identification import TrendState, VolatilityState
from trading.domain.contracts.paper_data import (
    P0_FIELDS,
    P1_FIELDS,
    PaperDataAssessment,
    PaperDataField,
    PaperDataPresence,
    PaperDataRequirements,
    PaperDataTier,
)
from trading.domain.contracts.snapshot import DerivativesContext, Greeks
from trading.domain.enums import OptionType, ReasonCode, RiskAction, TradeState
from trading.domain.ids import SequentialIdFactory
from trading.identification import (
    allowed_families_for,
    bind_long_option,
    blocked_families,
    load_identification_policy,
    observe_exit_depth,
    observe_p1_features,
    route_nifty_options,
)
from trading.identification.p1_features import ObservedP1Features
from trading.risk import CapitalReservationService, RiskGateway
from trading.runtime.paper_runner import PaperRunner
from trading.safety.paper_data import PaperDataInputs, assess_paper_data
from trading.storage.trading_store import TradingStore

ROOT = Path(__file__).resolve().parent.parent
REQUIREMENTS = load_paper_data_requirements(ROOT / "config" / "paper_data.yaml")
POLICY = load_identification_policy(ROOT / "config" / "identification.yaml")
NOW = f.NOW


def _p0_option(**overrides: object) -> FeatureSnapshot:
    payload: dict[str, object] = {
        "snapshot_id": "SNAP-P0",
        "contract": f.option_contract(),
        "times": f.snapshot_times(),
        "market": f.quote(
            bid=f.price("91.95"),
            ask=f.price("92.00"),
            last=f.price("92.00"),
            volume=5000,
            bid_size=300,
            ask_size=300,
        ),
        "derivatives": DerivativesContext(
            days_to_expiry=10,
            open_interest=5000,
            option_type=OptionType.CALL,
            underlying_price=f.price("24000"),
            greeks=Greeks(
                model="fixture",
                calculation_version="1",
                converged=True,
                implied_volatility=Decimal("15"),
                delta=Decimal("0.52"),
            ),
        ),
        "features": {"lot_size": Decimal(75), "top_of_book_observed": Decimal(1)},
    }
    payload.update(overrides)
    return f.snapshot(**payload)


def _inputs(
    snapshot: FeatureSnapshot | None = None,
    **overrides: object,
) -> PaperDataInputs:
    payload: dict[str, object] = {
        "now": NOW,
        "snapshots": (_p0_option() if snapshot is None else snapshot,),
        "event_risk": f.event_risk_state(),
        "portfolio": f.portfolio_snapshot(),
        "broker_state_ok": True,
        "margin_confirmed": True,
        "margin_required": f.money("6900"),
        "instruments": {"NIFTY26SEP24000CE": _instrument()},
    }
    payload.update(overrides)
    return PaperDataInputs(**payload)  # type: ignore[arg-type]


def _instrument() -> InstrumentSpec:
    return instrument_spec()


def _assess(
    inputs: PaperDataInputs,
    *,
    p1_present: frozenset[PaperDataField] = frozenset(),
    p1_absent: frozenset[PaperDataField] = P1_FIELDS,
) -> PaperDataAssessment:
    return assess_paper_data(
        REQUIREMENTS,
        inputs,
        p1_present=p1_present,
        p1_absent=p1_absent,
    )


def test_config_lists_every_p0_and_p1_field() -> None:
    assert {spec.field for spec in REQUIREMENTS.p0} == P0_FIELDS
    assert {spec.field for spec in REQUIREMENTS.p1} == P1_FIELDS
    assert REQUIREMENTS.spec_for(PaperDataField.LTP).max_age_ms == 120000
    assert REQUIREMENTS.windows.iv_skew.delta_min == Decimal("0.20")
    assert REQUIREMENTS.windows.realized_volatility.long_window_bars == 50
    assert REQUIREMENTS.windows.depth.observe_on_exit is True


def test_incomplete_p0_config_fails_closed() -> None:
    fields = [
        spec for spec in REQUIREMENTS.fields if spec.field is not PaperDataField.LTP
    ]
    with pytest.raises(ValueError, match="P0 paper-data contract is incomplete"):
        PaperDataRequirements.model_validate(
            {
                "requirements_version": "broken",
                "fields": [item.model_dump(mode="json") for item in fields],
                "windows": REQUIREMENTS.windows.model_dump(mode="json"),
            }
        )


def test_full_p0_happy_path_permits_entry() -> None:
    """Invariant 6: complete fresh P0 evidence does not block new exposure."""
    assessment = _assess(_inputs())
    assert assessment.p0_ok
    assert assessment.failed_p0_fields == ()
    assert assessment.p0_reason_codes == ()


@pytest.mark.parametrize(
    ("field", "inputs", "presence", "reason"),
    [
        (
            PaperDataField.LTP,
            lambda: _inputs(
                _p0_option(
                    market=f.quote(
                        bid=f.price("91.95"),
                        ask=f.price("92.00"),
                        last=None,
                        volume=5000,
                    )
                )
            ),
            PaperDataPresence.MISSING,
            ReasonCode.PRICE_UNAVAILABLE,
        ),
        (
            PaperDataField.LTP,
            lambda: _inputs(
                _p0_option(
                    market=f.quote(
                        bid=f.price("91.95"),
                        ask=f.price("92.00"),
                        last=f.price("0.00"),
                        volume=5000,
                    )
                )
            ),
            PaperDataPresence.ZERO_INVALID,
            ReasonCode.PRICE_UNAVAILABLE,
        ),
        (
            PaperDataField.BID_ASK,
            lambda: _inputs(
                _p0_option(
                    market=f.quote(
                        bid=None,
                        ask=f.price("92.00"),
                        last=f.price("92.00"),
                        volume=5000,
                    )
                )
            ),
            PaperDataPresence.MISSING,
            ReasonCode.PRICE_UNAVAILABLE,
        ),
        (
            PaperDataField.QUOTE_FRESHNESS,
            lambda: _inputs(
                now=NOW + timedelta(minutes=3),
            ),
            PaperDataPresence.STALE,
            ReasonCode.DATA_STALE,
        ),
        (
            PaperDataField.VOLUME,
            lambda: _inputs(
                _p0_option(
                    market=f.quote(
                        bid=f.price("91.95"),
                        ask=f.price("92.00"),
                        last=f.price("92.00"),
                    )
                )
            ),
            PaperDataPresence.MISSING,
            ReasonCode.DATA_GAP,
        ),
        (
            PaperDataField.OPEN_INTEREST,
            lambda: _inputs(
                _p0_option(
                    derivatives=DerivativesContext(
                        days_to_expiry=10,
                        open_interest=None,
                        option_type=OptionType.CALL,
                        underlying_price=f.price("24000"),
                    )
                )
            ),
            PaperDataPresence.MISSING,
            ReasonCode.DEPTH_INSUFFICIENT,
        ),
        (
            PaperDataField.OPEN_INTEREST,
            lambda: _inputs(
                _p0_option(
                    derivatives=DerivativesContext(
                        days_to_expiry=10,
                        open_interest=0,
                        option_type=OptionType.CALL,
                        underlying_price=f.price("24000"),
                    )
                )
            ),
            PaperDataPresence.ZERO_INVALID,
            ReasonCode.DEPTH_INSUFFICIENT,
        ),
        (
            PaperDataField.CONTRACT_METADATA,
            lambda: _inputs(
                _p0_option(features={"top_of_book_observed": Decimal(1)}),
                instruments={},
            ),
            PaperDataPresence.ZERO_INVALID,
            ReasonCode.INSTRUMENT_UNKNOWN,
        ),
        (
            PaperDataField.MARGIN_ESTIMATE,
            lambda: _inputs(margin_confirmed=None, margin_required=None),
            PaperDataPresence.MISSING,
            ReasonCode.MARGIN_INSUFFICIENT,
        ),
        (
            PaperDataField.MARGIN_ESTIMATE,
            lambda: _inputs(margin_confirmed=True, margin_required=f.money("0")),
            PaperDataPresence.ZERO_INVALID,
            ReasonCode.MARGIN_INSUFFICIENT,
        ),
        (
            PaperDataField.POSITION_BROKER_STATE,
            lambda: _inputs(portfolio=None),
            PaperDataPresence.MISSING,
            ReasonCode.RECONCILIATION_UNRESOLVED,
        ),
        (
            PaperDataField.POSITION_BROKER_STATE,
            lambda: _inputs(broker_state_ok=False),
            PaperDataPresence.MISSING,
            ReasonCode.RECONCILIATION_UNRESOLVED,
        ),
        (
            PaperDataField.EVENT_STATE,
            lambda: _inputs(event_risk=None),
            PaperDataPresence.MISSING,
            ReasonCode.EVENT_BLACKOUT,
        ),
        (
            PaperDataField.EVENT_STATE,
            lambda: _inputs(now=NOW + timedelta(hours=2)),
            PaperDataPresence.STALE,
            ReasonCode.DATA_STALE,
        ),
    ],
    ids=(
        "ltp-missing",
        "ltp-zero",
        "bid-ask-missing",
        "quote-stale",
        "volume-missing",
        "oi-missing",
        "oi-zero",
        "metadata-no-lot",
        "margin-missing",
        "margin-zero",
        "portfolio-missing",
        "broker-blocked",
        "event-missing",
        "event-stale",
    ),
)
def test_each_p0_breach_blocks_entry(
    field: PaperDataField,
    inputs: Callable[[], PaperDataInputs],
    presence: PaperDataPresence,
    reason: ReasonCode,
) -> None:
    """Invariant 6: any P0 hole blocks paper entry with a reason code."""
    assessment = _assess(inputs())
    assert not assessment.p0_ok
    assert field in assessment.failed_p0_fields
    row = next(item for item in assessment.results if item.field is field)
    assert row.presence is presence
    assert row.reason_code is reason
    assert reason in assessment.p0_reason_codes


@pytest.fixture
def gateway(tmp_path: Path) -> RiskGateway:
    clock = FrozenClock(NOW)
    ids = SequentialIdFactory(clock.instant)
    store = TradingStore.open(tmp_path / "trading.db", clock=clock)
    broker = PaperBroker.from_fixtures(BROKER_FIXTURES, clock=clock, id_factory=ids)
    return RiskGateway(
        account_config=ACCOUNT_CONFIG,
        risk_policy=RISK_POLICY,
        reservation_service=CapitalReservationService(
            store, clock=clock, id_factory=ids
        ),
        margin_preview=broker,
        clock=clock,
        id_factory=ids,
    )


def test_gateway_rejects_when_p0_volume_missing(gateway: RiskGateway) -> None:
    snap = option_snapshot()
    request = replace(
        _gateway_request(feature_snapshot=snap),
        paper_requirements=REQUIREMENTS,
    )
    decision = gateway.evaluate(request)
    assert decision.action is RiskAction.REJECT
    assert ReasonCode.DATA_GAP in decision.reason_codes


def test_gateway_approves_full_p0(gateway: RiskGateway) -> None:
    snap = option_snapshot(
        market=f.quote(
            bid=f.price("91.95"),
            ask=f.price("92.00"),
            last=f.price("92.00"),
            volume=4000,
            bid_size=300,
            ask_size=300,
        )
    )
    request = replace(
        _gateway_request(feature_snapshot=snap),
        paper_requirements=REQUIREMENTS,
    )
    decision = gateway.evaluate(request)
    assert decision.action is RiskAction.APPROVE


def test_paper_runner_blocks_entry_when_p0_volume_missing(
    tmp_path: Path,
) -> None:
    clock = FrozenClock(NOW + timedelta(seconds=60))
    store = TradingStore.open(tmp_path / "paper.sqlite", clock=clock)
    ids = SequentialIdFactory(clock.instant)
    runner = PaperRunner(
        account_config=_paper_config(),  # type: ignore[arg-type]
        risk_policy=load_risk_policy(ROOT / "config" / "risk.yaml"),
        store=store,
        broker=PaperBroker.from_fixtures(BROKER_FIXTURES, clock=clock, id_factory=ids),
        clock=clock,
        id_factory=ids,
        paper_data_requirements=REQUIREMENTS,
    )
    result = runner.run_cycle((_request(),))
    outcome = result.outcomes[0]
    assert outcome.order_events == ()
    assert ReasonCode.DATA_GAP in outcome.entry_blocked_reasons
    store.close()


def _id_option(
    symbol: str,
    *,
    strike: str,
    delta: str,
    iv: str,
    option_type: OptionType = OptionType.CALL,
    bid_size: int | None = None,
    ask_size: int | None = None,
    expiry: date | None = None,
    theta: str | None = None,
    vega: str | None = None,
    dte: int = 5,
) -> FeatureSnapshot:
    return f.snapshot(
        snapshot_id=f"snapshot-{symbol}",
        contract=f.option_contract(
            symbol=symbol,
            expiry=expiry or date(2026, 9, 24),
            strike=Decimal(strike),
            option_type=option_type,
        ),
        market=f.quote(
            bid=f.price("99"),
            ask=f.price("100"),
            last=f.price("99"),
            volume=5000,
            bid_size=bid_size,
            ask_size=ask_size,
        ),
        derivatives=DerivativesContext(
            days_to_expiry=dte,
            open_interest=5000,
            option_type=option_type,
            greeks=Greeks(
                model="fixture",
                calculation_version="1",
                converged=True,
                implied_volatility=Decimal(iv),
                delta=Decimal(delta),
                theta=None if theta is None else Decimal(theta),
                vega=None if vega is None else Decimal(vega),
            ),
            underlying_price=f.price("24000"),
        ),
        features={"lot_size": Decimal(75), "top_of_book_observed": Decimal(1)},
    )


def _market_state() -> MarketState:
    return _market(trend=TrendState.UP)


def test_p1_surface_changes_long_option_selection() -> None:
    """Cheaper observed IV wins only when a real surface is present."""
    candidates = (
        _id_option("AAA-24000-CE", strike="24000", delta="0.52", iv="20"),
        _id_option("ZZZ-24000-CE", strike="24000", delta="0.52", iv="12"),
        _id_option("NIFTY-23900-CE", strike="23900", delta="0.70", iv="15"),
        _id_option("NIFTY-24100-CE", strike="24100", delta="0.15", iv="15"),
    )
    market = _market_state()
    without = bind_long_option(candidates, market=market, policy=POLICY)
    observed = observe_p1_features(candidates, requirements=REQUIREMENTS, market=market)
    assert PaperDataField.IV_SURFACE in observed.present
    with_p1 = bind_long_option(candidates, market=market, policy=POLICY, p1=observed)
    assert without.binding.selected_symbols == ("AAA-24000-CE",)
    assert with_p1.binding.selected_symbols == ("ZZZ-24000-CE",)
    assert with_p1.setup_features is not None
    assert "IV_SURFACE" in with_p1.setup_features.p1_fields_used


def test_p1_absent_does_not_invent_or_crash() -> None:
    candidates = (
        _id_option("AAA-24000-CE", strike="24000", delta="0.52", iv="20"),
        _id_option("ZZZ-24000-CE", strike="24000", delta="0.52", iv="12"),
    )
    market = _market_state()
    observed = observe_p1_features(candidates, requirements=REQUIREMENTS, market=None)
    assert PaperDataField.IV_SURFACE in observed.absent
    assert observed.iv_skew is None
    assert observed.term_atm_iv == ()
    assert observed.realized_volatility_annualized is None
    assert PaperDataField.DEPTH in observed.absent
    bound = bind_long_option(candidates, market=market, policy=POLICY, p1=observed)
    baseline = bind_long_option(candidates, market=market, policy=POLICY)
    assert bound.binding.selected_symbols == baseline.binding.selected_symbols
    assert bound.setup_features is not None
    assert "IV_SURFACE" in bound.setup_features.p1_fields_absent
    assert "IV_SKEW" in bound.setup_features.p1_fields_absent
    assert blocked_families(observed, REQUIREMENTS) == frozenset({"cas_microstructure"})


def test_p1_skew_is_observed_not_invented() -> None:
    candidates = (
        _id_option("C25", strike="24200", delta="0.25", iv="14"),
        _id_option(
            "P25",
            strike="23800",
            delta="-0.25",
            iv="18",
            option_type=OptionType.PUT,
        ),
        _id_option("C50", strike="24000", delta="0.52", iv="15"),
        _id_option(
            "P50",
            strike="24000",
            delta="-0.50",
            iv="16",
            option_type=OptionType.PUT,
        ),
    )
    observed = observe_p1_features(candidates, requirements=REQUIREMENTS)
    assert observed.iv_skew == Decimal("4.000000")
    empty = observe_p1_features(candidates[:1], requirements=REQUIREMENTS)
    assert empty.iv_skew is None


def test_observed_p1_can_be_merged_into_assessment() -> None:
    observed = ObservedP1Features(
        points=(),
        iv_skew=None,
        term_atm_iv=(),
        atm_iv_by_expiry=(),
        term_slope=None,
        realized_volatility_annualized=Decimal("15"),
        realized_volatility_ratio=Decimal("1"),
        depth_top_size=(),
        present=(PaperDataField.REALIZED_VOLATILITY,),
        absent=tuple(
            field
            for field in P1_FIELDS
            if field is not PaperDataField.REALIZED_VOLATILITY
        ),
        absence_reasons=((PaperDataField.IV_SURFACE, "fewer_than_min_points"),),
    )
    assessment = assess_paper_data(
        REQUIREMENTS,
        _inputs(),
        p1_present=frozenset(observed.present),
        p1_absent=frozenset(observed.absent),
    )
    assert assessment.p0_ok
    assert PaperDataField.REALIZED_VOLATILITY in assessment.p1_present
    assert PaperDataField.IV_SURFACE in assessment.p1_absent
    row = next(
        item for item in assessment.results if item.field is PaperDataField.IV_SURFACE
    )
    assert row.detail == "p1_absent_not_invented"
    assert row.tier is PaperDataTier.P1


def test_missing_p1_windows_fail_closed() -> None:
    payload = REQUIREMENTS.model_dump(mode="json")
    payload.pop("windows")
    with pytest.raises(ValueError):
        PaperDataRequirements.model_validate(payload)


def test_p1_records_absence_reasons_without_inventing() -> None:
    observed = observe_p1_features(
        (_id_option("AAA-24000-CE", strike="24000", delta="0.52", iv="20"),),
        requirements=REQUIREMENTS,
        market=None,
    )
    labels = observed.reason_labels()
    assert "IV_SURFACE:fewer_than_min_points" in labels
    assert "IV_SKEW:missing_25d_put_or_call" in labels
    assert "TERM_STRUCTURE:fewer_than_min_expiries" in labels
    assert "REALIZED_VOLATILITY:missing_or_unwarmed_history" in labels
    assert "DEPTH:missing_top_of_book_size" in labels
    bound = bind_long_option(
        (
            _id_option("AAA-24000-CE", strike="24000", delta="0.52", iv="20"),
            _id_option("ZZZ-24000-CE", strike="24000", delta="0.52", iv="12"),
        ),
        market=_market_state(),
        policy=POLICY,
        p1=observed,
    )
    assert bound.setup_features is not None
    assert bound.setup_features.p1_absence_reasons == labels


def test_p1_term_structure_prefers_cheaper_atm_expiry() -> None:
    """Front ATM cheaper than back only ranks when two expiries are observed."""
    near = date(2026, 9, 24)
    far = date(2026, 10, 29)
    candidates = (
        _id_option("NEAR-CHEAP", strike="24000", delta="0.52", iv="11", expiry=near),
        _id_option(
            "FAR-RICH", strike="24000", delta="0.52", iv="22", expiry=far, dte=28
        ),
        _id_option("NEAR-OTM", strike="24200", delta="0.20", iv="12", expiry=near),
        _id_option(
            "FAR-OTM", strike="24200", delta="0.20", iv="21", expiry=far, dte=28
        ),
    )
    market = _market_state()
    without = bind_long_option(candidates, market=market, policy=POLICY)
    observed = observe_p1_features(candidates, requirements=REQUIREMENTS, market=market)
    assert PaperDataField.TERM_STRUCTURE in observed.present
    assert observed.term_slope is not None
    with_p1 = bind_long_option(candidates, market=market, policy=POLICY, p1=observed)
    assert without.binding.selected_symbols == ("FAR-RICH",)
    assert with_p1.binding.selected_symbols == ("NEAR-CHEAP",)


def test_p1_depth_prefers_deeper_book_when_sizes_are_observed() -> None:
    """Invariant 6: missing size is not filled; observed size changes ranking."""
    thin = _id_option(
        "AAA-THIN", strike="24000", delta="0.52", iv="15", bid_size=10, ask_size=10
    )
    deep = _id_option(
        "ZZZ-DEEP", strike="24000", delta="0.52", iv="15", bid_size=800, ask_size=800
    )
    filler_a = _id_option(
        "FILL-A", strike="23900", delta="0.70", iv="15", bid_size=50, ask_size=50
    )
    filler_b = _id_option(
        "FILL-B", strike="24100", delta="0.15", iv="15", bid_size=50, ask_size=50
    )
    candidates = (thin, deep, filler_a, filler_b)
    market = _market_state()
    without = bind_long_option(candidates, market=market, policy=POLICY)
    observed = observe_p1_features(candidates, requirements=REQUIREMENTS, market=market)
    assert PaperDataField.DEPTH in observed.present
    with_p1 = bind_long_option(candidates, market=market, policy=POLICY, p1=observed)
    assert without.binding.selected_symbols == ("AAA-THIN",)
    assert with_p1.binding.selected_symbols == ("ZZZ-DEEP",)


def test_p1_greeks_prefer_slower_theta_decay_when_theta_is_observed() -> None:
    slow = _id_option("SLOW-THETA", strike="24000", delta="0.52", iv="15", theta="-1")
    fast = _id_option("FAST-THETA", strike="24000", delta="0.52", iv="15", theta="-20")
    filler_a = _id_option("FILL-A", strike="23900", delta="0.70", iv="15", theta="-5")
    filler_b = _id_option("FILL-B", strike="24100", delta="0.15", iv="15", theta="-5")
    candidates = (fast, slow, filler_a, filler_b)
    market = _market_state()
    without = bind_long_option(candidates, market=market, policy=POLICY)
    observed = observe_p1_features(candidates, requirements=REQUIREMENTS, market=market)
    assert PaperDataField.GREEKS in observed.present
    with_p1 = bind_long_option(candidates, market=market, policy=POLICY, p1=observed)
    assert without.binding.selected_symbols == ("FAST-THETA",)
    assert with_p1.binding.selected_symbols == ("SLOW-THETA",)


def test_p1_rv_absent_when_history_is_shorter_than_the_long_window() -> None:
    market = _market(
        completed_bar_count=10, realized_volatility_annualized=Decimal("15")
    )
    observed = observe_p1_features(
        (
            _id_option("AAA-24000-CE", strike="24000", delta="0.52", iv="15"),
            _id_option("ZZZ-24000-CE", strike="24000", delta="0.52", iv="12"),
            _id_option("NIFTY-23900-CE", strike="23900", delta="0.70", iv="15"),
            _id_option("NIFTY-24100-CE", strike="24100", delta="0.15", iv="15"),
        ),
        requirements=REQUIREMENTS,
        market=market,
    )
    assert PaperDataField.REALIZED_VOLATILITY in observed.absent
    assert "REALIZED_VOLATILITY:fewer_than_long_window_bars" in observed.reason_labels()


def test_router_uses_observed_expanding_rv_to_prefer_debit_spread() -> None:
    long_option = _bound("positional_long_option", "0.90")
    spread = _bound("debit_spread", "0.80")
    cheap_iv = _market(
        iv_percentile=Decimal("30"),
        iv_rv_ratio=Decimal("1.0"),
        volatility=VolatilityState.EXPANDING,
        realized_volatility_ratio=Decimal("1.40"),
    )
    without, _ = route_nifty_options(
        cheap_iv, long_option=long_option, debit_spread=spread, policy=POLICY
    )
    observed = ObservedP1Features(
        points=(),
        iv_skew=None,
        term_atm_iv=(),
        atm_iv_by_expiry=(),
        term_slope=None,
        realized_volatility_annualized=Decimal("18"),
        realized_volatility_ratio=Decimal("1.40"),
        depth_top_size=(),
        present=(PaperDataField.REALIZED_VOLATILITY,),
        absent=(),
        absence_reasons=(),
    )
    with_p1, _ = route_nifty_options(
        cheap_iv,
        long_option=long_option,
        debit_spread=spread,
        policy=POLICY,
        p1=observed,
    )
    assert without.paper_winner == "positional_long_option"
    assert with_p1.paper_winner == "debit_spread"


def test_allow_table_blocks_cas_when_p1_depth_is_absent() -> None:
    market = _market_state()
    observed = observe_p1_features(
        (_id_option("AAA-24000-CE", strike="24000", delta="0.52", iv="20"),),
        requirements=REQUIREMENTS,
    )
    allowed = allowed_families_for(market, POLICY, p1=observed, paper_data=REQUIREMENTS)
    assert "cas_microstructure" not in allowed


def test_allow_table_allows_cas_when_p1_depth_is_present() -> None:
    """Invariant 6: observed size unblocks CAS; missing size is never filled."""
    candidates = (
        _id_option(
            "AAA-24000-CE",
            strike="24000",
            delta="0.52",
            iv="20",
            bid_size=100,
            ask_size=100,
        ),
    )
    market = _market_state()
    observed = observe_p1_features(candidates, requirements=REQUIREMENTS)
    assert PaperDataField.DEPTH in observed.present
    allowed = allowed_families_for(market, POLICY, p1=observed, paper_data=REQUIREMENTS)
    assert "cas_microstructure" in allowed


def test_exit_depth_gap_does_not_block_software_stop(tmp_path: Path) -> None:
    """Invariant 6: missing exit depth is logged; existing protection still fires."""
    clock = FrozenClock(NOW + timedelta(seconds=60))
    store = TradingStore.open(tmp_path / "paper.sqlite", clock=clock)
    ids = SequentialIdFactory(clock.instant)
    runner = PaperRunner(
        account_config=_paper_config(),  # type: ignore[arg-type]
        risk_policy=load_risk_policy(ROOT / "config" / "risk.yaml"),
        store=store,
        broker=PaperBroker.from_fixtures(BROKER_FIXTURES, clock=clock, id_factory=ids),
        clock=clock,
        id_factory=ids,
        paper_data_requirements=REQUIREMENTS,
    )
    option = _request().candidates[0]
    complete = option.model_copy(
        update={
            "market": f.quote(
                bid=f.price("91.95"),
                ask=f.price("92.00"),
                last=f.price("92.00"),
                volume=4000,
                bid_size=300,
                ask_size=300,
            )
        }
    )
    opened = runner.run_cycle((_request(candidates=(complete,)),))
    assert opened.outcomes[0].order_events
    position = runner.trade_manager.list_positions()[0]
    symbol = position.legs[0].contract.symbol
    stop = _option_snapshot(
        position.legs[0].contract,
        market=f.quote(bid=f.price("1.00"), ask=f.price("1.05")),
    )
    present, reason = observe_exit_depth(stop, REQUIREMENTS)
    assert present is False
    assert reason == "missing_top_of_book_size"
    events = runner.manage_exits({symbol: stop})
    assert events
    assert runner.exit_depth_gaps == (f"{symbol}:missing_top_of_book_size",)
    closed = runner.trade_manager.get_position(position.trade_id)
    assert closed is not None
    assert closed.state is TradeState.CLOSED
    store.close()


def test_depth_helpers_do_not_invent_missing_sizes() -> None:
    empty = CanonicalMarketEvent(
        event_id="depth-empty",
        provider="fyers",
        symbol="NSE:NIFTY26SEP24000CE",
        event_type="DEPTH_SNAPSHOT",
        event_time=NOW,
        source_time=NOW,
        receive_time=NOW,
        provider_sequence=None,
        payload={"bid_levels": [], "ask_levels": []},
        raw_ref="raw",
        normalization_version="1",
    )
    assert depth_top_sizes(empty) == (None, None)
    assert observed_book_sizes({"bid": 100, "ask": 101, "volume": 12}) == (None, None)
    assert observed_book_sizes({"bid_size": 40, "ask_size": 55}) == (40, 55)
