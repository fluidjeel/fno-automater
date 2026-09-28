"""DISC-A28: DISCOVERY resolves instruments instead of INSTRUMENT_UNKNOWN."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import tests.factories as f
from tests.test_risk_gateway import (
    ACCOUNT_CONFIG,
    BROKER_FIXTURES,
    NOW,
    RISK_POLICY,
    instrument_spec,
    option_snapshot,
)
from trading.broker.paper import PaperBroker
from trading.data.events import CanonicalMarketEvent
from trading.data.storage.instrument_store import InstrumentSpecStore
from trading.domain.clock import FrozenClock
from trading.domain.contracts import DerivativesContext, IntentLeg
from trading.domain.enums import (
    Exchange,
    ExecutionMode,
    InstrumentKind,
    OptionType,
    ReasonCode,
    RiskAction,
    Side,
)
from trading.domain.ids import SequentialIdFactory
from trading.risk import RiskGateway, RiskGatewayRequest
from trading.risk.instrument_registry import InstrumentRegistry
from trading.risk.instrument_resolve import resolve_discovery_risk_context
from trading.risk.reservation import CapitalReservationService
from trading.runtime.candidates import build_option_candidates
from trading.runtime.paper_runner import PaperStrategyRequest
from trading.storage import TradingStore

ZONE = ZoneInfo("Asia/Kolkata")


def _following_week_spec() -> dict[str, object]:
    return {
        "trading_symbol": "NSE:NIFTY26OCT24500CE",
        "exchange": Exchange.NFO,
        "segment": "NSE_FO",
        "underlying": "NIFTY",
        "instrument_kind": InstrumentKind.OPTION,
        "provider_token": "fw-1",
        "exchange_token": 42,
        "lot_size": 75,
        "tick_size": Decimal("0.05"),
        "price_precision": 2,
        "expiry": date(2026, 10, 6),
        "strike": Decimal("24500"),
        "option_type": OptionType.CALL,
        "trading_session": "0915-1530",
        "source": "fixture",
        "verified_at": date(2026, 9, 28),
    }


def _following_week_chain() -> CanonicalMarketEvent:
    return CanonicalMarketEvent(
        event_id="chain-fw",
        provider="fyers",
        symbol="NSE:NIFTY50-INDEX",
        event_type="OPTION_CHAIN_SNAPSHOT",
        event_time=NOW,
        source_time=NOW,
        receive_time=NOW,
        provider_sequence=1,
        payload={
            "strikes": [
                {
                    "symbol": "NSE:NIFTY26OCT24500CE",
                    "strike_price": 24500,
                    "option_type": "CE",
                    "ltp": 120,
                    "bid": 119.5,
                    "ask": 120.5,
                    "oi": 2000,
                }
            ]
        },
        normalization_version="1",
        raw_ref="test",
    )


def test_following_week_chain_resolves_missing_catalog_row(tmp_path: Path) -> None:
    """Following-week symbols missing from disk catalog resolve via registry."""
    store = InstrumentSpecStore(tmp_path)
    fw_spec = instrument_spec(**_following_week_spec())
    registry = InstrumentRegistry(
        store,
        fetch_master=lambda symbols: (
            (fw_spec,) if "NSE:NIFTY26OCT24500CE" in symbols else ()
        ),
    )
    underlying = f.snapshot(contract=f.index_contract())
    candidates, specs = build_option_candidates(
        _following_week_chain(),
        store,
        underlying=underlying,
        as_of=NOW,
        zone=ZONE,
        strikes_each_side=5,
        registry=registry,
    )
    assert len(candidates) == 1
    assert specs["NSE:NIFTY26OCT24500CE"].lot_size == 75
    assert store.find("NSE:NIFTY26OCT24500CE") is not None


def test_registry_rebuild_after_restart_resolves_symbols(tmp_path: Path) -> None:
    """Restart rebuild reloads merged master rows for risk resolution."""
    store = InstrumentSpecStore(tmp_path)
    fw_spec = instrument_spec(**_following_week_spec())
    registry = InstrumentRegistry(
        store,
        fetch_master=lambda symbols: (fw_spec,) if symbols else (),
    )
    registry.ensure(frozenset({"NSE:NIFTY26OCT24500CE"}))
    registry.rebuild()
    assert registry.get("NSE:NIFTY26OCT24500CE") is not None
    assert registry.get("NSE:NIFTY26OCT24500CE") is not None


def test_strict_gateway_unchanged_for_unclassifiable_structure(
    tmp_path: Path, clock: FrozenClock
) -> None:
    """STRICT still rejects unclassifiable structures as INSTRUMENT_UNKNOWN."""
    ids = SequentialIdFactory(clock.instant)
    store = TradingStore.open(tmp_path / "trading.db", clock=clock)
    broker = PaperBroker.from_fixtures(BROKER_FIXTURES, clock=clock, id_factory=ids)
    gateway = RiskGateway(
        account_config=ACCOUNT_CONFIG,
        risk_policy=RISK_POLICY,
        reservation_service=CapitalReservationService(
            store, clock=clock, id_factory=ids
        ),
        margin_preview=broker,
        clock=clock,
        id_factory=ids,
    )
    snap = option_snapshot()
    intent = f.intent(
        snapshot_id=snap.snapshot_id,
        legs=(
            IntentLeg(
                leg_id="leg-1",
                contract=snap.contract,
                side=Side.SELL,
                ratio=1,
            ),
        ),
    )
    decision = gateway.evaluate(
        RiskGatewayRequest(
            intent=intent,
            feature_snapshot=snap,
            portfolio_snapshot=f.portfolio_snapshot(),
            instrument=instrument_spec(),
            event_risk_state=f.event_risk_state(),
        )
    )
    assert decision.action is RiskAction.REJECT
    assert decision.reason_codes == (ReasonCode.INSTRUMENT_UNKNOWN,)


def test_should_resolve_instruments_only_for_discovery() -> None:
    from trading.domain.enums import EntryProfile
    from trading.risk.instrument_resolve import should_resolve_instruments

    assert should_resolve_instruments(EntryProfile.DISCOVERY) is True
    assert should_resolve_instruments(EntryProfile.STRICT) is False


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(NOW)


def test_discovery_risk_context_resolves_instrument_for_intent(
    tmp_path: Path, clock: FrozenClock
) -> None:
    """DISCOVERY resolves specs from registry before risk evaluation."""
    store = InstrumentSpecStore(tmp_path)
    spec = instrument_spec(**_following_week_spec())
    registry = InstrumentRegistry(store, fetch_master=lambda symbols: (spec,))
    snap = option_snapshot(
        contract=f.option_contract(
            symbol="NSE:NIFTY26OCT24500CE",
            expiry=date(2026, 10, 6),
            strike=Decimal("24500"),
        ),
        derivatives=DerivativesContext(
            days_to_expiry=8,
            open_interest=2000,
            option_type=OptionType.CALL,
            underlying_price=f.price("24500"),
        ),
    )
    intent = f.intent(
        snapshot_id=snap.snapshot_id,
        legs=(
            IntentLeg(
                leg_id="leg-1",
                contract=snap.contract,
                side=Side.BUY,
                ratio=1,
            ),
        ),
    )
    request = PaperStrategyRequest(
        strategy_id="positional_long_option",
        underlying=f.snapshot(contract=f.index_contract()),
        candidates=(snap,),
        instruments={},
        event_risk_state=None,
        experiment_id="EXP-A28",
        execution_mode=ExecutionMode.PAPER,
        macro=None,
        execute=True,
    )
    resolved = resolve_discovery_risk_context(
        intent,
        candidates=request.candidates,
        instruments=request.instruments,
        underlying=request.underlying,
        registry=registry,
    )
    assert not isinstance(resolved, ReasonCode)
    assert resolved.instrument.trading_symbol == "NSE:NIFTY26OCT24500CE"
    assert resolved.feature_snapshot.derivatives is not None
