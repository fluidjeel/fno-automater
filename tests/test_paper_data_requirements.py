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
from tests.test_identification import _market
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
from trading.domain.clock import FrozenClock
from trading.domain.contracts import FeatureSnapshot, InstrumentSpec, MarketState
from trading.domain.contracts.identification import TrendState
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
from trading.domain.enums import OptionType, ReasonCode, RiskAction
from trading.domain.ids import SequentialIdFactory
from trading.identification import (
    bind_long_option,
    blocked_families,
    load_identification_policy,
    observe_p1_features,
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


def test_incomplete_p0_config_fails_closed() -> None:
    fields = [
        spec for spec in REQUIREMENTS.fields if spec.field is not PaperDataField.LTP
    ]
    with pytest.raises(ValueError, match="P0 paper-data contract is incomplete"):
        PaperDataRequirements.model_validate(
            {
                "requirements_version": "broken",
                "fields": [item.model_dump(mode="json") for item in fields],
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
    broker = PaperBroker.from_fixtures(
        BROKER_FIXTURES, clock=clock, id_factory=ids
    )
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
        broker=PaperBroker.from_fixtures(
            BROKER_FIXTURES, clock=clock, id_factory=ids
        ),
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
) -> FeatureSnapshot:
    return f.snapshot(
        snapshot_id=f"snapshot-{symbol}",
        contract=f.option_contract(
            symbol=symbol,
            expiry=date(2026, 9, 24),
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
            days_to_expiry=5,
            open_interest=5000,
            option_type=option_type,
            greeks=Greeks(
                model="fixture",
                calculation_version="1",
                converged=True,
                implied_volatility=Decimal(iv),
                delta=Decimal(delta),
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
        realized_volatility_annualized=Decimal("15"),
        realized_volatility_ratio=Decimal("1"),
        present=(PaperDataField.REALIZED_VOLATILITY,),
        absent=tuple(
            field
            for field in P1_FIELDS
            if field is not PaperDataField.REALIZED_VOLATILITY
        ),
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
