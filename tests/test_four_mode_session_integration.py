"""Four-mode session integration: binding → risk → fill → position → recovery."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest

import tests.factories as f
from tests.test_paper_runner import BROKER_FIXTURES, ROOT, _paper_config
from trading.broker.paper import PaperBroker
from trading.broker.ports import MarginPreviewRequest, MarginPreviewResult
from trading.config import LoadedConfig, load_evaluation_config, load_risk_policy
from trading.domain.clock import FrozenClock
from trading.domain.contracts import DerivativesContext, FeatureSnapshot, InstrumentSpec
from trading.domain.contracts.mode_policy import load_modes_config
from trading.domain.enums import (
    Exchange,
    ExecutionMode,
    FamilyId,
    InstrumentKind,
    ModeId,
    OptionType,
    OrderState,
    ReasonCode,
    RiskAction,
    TradeState,
)
from trading.domain.ids import SequentialIdFactory
from trading.risk import CapitalReservationService, RiskGateway
from trading.runtime.paper_runner import PaperRunner, PaperStrategyRequest
from trading.runtime.paper_session import (
    load_paper_session_config,
)
from trading.runtime.session_routing import SessionRoutingProfile
from trading.runtime.startup_validation import validate_startup_configuration
from trading.storage.trading_store import TradingStore
from trading.strategies.macro import MacroAssessment, MacroBias

NOW = f.NOW
MODES = load_modes_config(ROOT / "config" / "modes.yaml")


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(NOW + timedelta(seconds=60))


@pytest.fixture
def store(tmp_path: Path, clock: FrozenClock) -> Iterator[TradingStore]:
    trading_store = TradingStore.open(tmp_path / "four_mode_int.sqlite", clock=clock)
    yield trading_store
    trading_store.close()


def _underlying(last: str = "24050") -> FeatureSnapshot:
    return f.snapshot(
        snapshot_id="SNAP-UNDER",
        contract=f.index_contract(),
        market=f.quote(last=f.price(last), close=f.price("24000")),
    )


def _option_snap(
    strike: str = "24000",
    option_type: OptionType = OptionType.CALL,
    ask: str = "50.00",
) -> FeatureSnapshot:
    suffix = "CE" if option_type is OptionType.CALL else "PE"
    return f.snapshot(
        snapshot_id=f"SNAP-{strike}-{suffix}",
        contract=f.option_contract(
            symbol=f"NIFTY26OCT{strike}{suffix}",
            strike=Decimal(strike),
            option_type=option_type,
        ),
        market=f.quote(
            bid=f.price(str(Decimal(ask) - Decimal("0.05"))),
            ask=f.price(ask),
            last=f.price(ask),
            bid_size=300,
            ask_size=300,
        ),
        derivatives=DerivativesContext(
            days_to_expiry=10,
            open_interest=5000,
            option_type=option_type,
            underlying_price=f.price("24000"),
        ),
    )


def _spec(symbol: str, strike: str, option_type: OptionType) -> InstrumentSpec:
    return InstrumentSpec.model_validate(
        {
            "trading_symbol": f"NSE:{symbol}",
            "exchange": Exchange.NFO,
            "segment": "NSE_FO",
            "underlying": "NIFTY",
            "instrument_kind": InstrumentKind.OPTION,
            "provider_token": "1",
            "exchange_token": 1,
            "lot_size": 75,
            "tick_size": Decimal("0.05"),
            "price_precision": 2,
            "expiry": date(2026, 10, 1),
            "strike": Decimal(strike),
            "option_type": option_type,
            "trading_session": "0915-1530",
            "source": "fixture",
            "verified_at": date(2026, 9, 11),
        }
    )


class _ConfirmedMarginPreview:
    def preview_margin(self, request: MarginPreviewRequest) -> MarginPreviewResult:
        return MarginPreviewResult(
            request_id=request.request_id,
            as_of=NOW,
            margin_required=f.money("5000"),
            margin_available_after=f.money("5000000"),
            confirmed=True,
        )


def _runner(store: TradingStore, clock: FrozenClock) -> PaperRunner:
    ids = SequentialIdFactory(clock.instant)
    fill_model = load_evaluation_config(
        ROOT / "config" / "evaluation.yaml"
    ).config.fill_model
    broker = PaperBroker.from_fixtures(
        BROKER_FIXTURES, clock=clock, id_factory=ids, fill_model=fill_model
    )
    account = cast(LoadedConfig, _paper_config())
    risk = load_risk_policy(ROOT / "config" / "risk.yaml")
    reservations = CapitalReservationService(store, clock=clock, id_factory=ids)
    from trading.risk.mode_ledger import FourModeBook
    from zoneinfo import ZoneInfo

    session_date = clock.now_utc().astimezone(ZoneInfo("Asia/Kolkata")).date()
    mode_book = FourModeBook.reconstruct_from_store(
        store, session_date, modes_config=MODES
    )
    runner = PaperRunner(
        account_config=account,
        risk_policy=risk,
        store=store,
        broker=broker,
        clock=clock,
        id_factory=ids,
        fill_model=fill_model,
    )
    runner._services.gateway = RiskGateway(
        account_config=account,
        risk_policy=risk,
        reservation_service=reservations,
        margin_preview=_ConfirmedMarginPreview(),
        clock=clock,
        id_factory=ids,
        modes_config=MODES,
        mode_book=mode_book,
    )
    return runner


def test_four_mode_config_loads_and_validates() -> None:
    cfg = load_paper_session_config(ROOT / "config" / "paper_session_four_mode.yaml")
    assert cfg.routing_profile is SessionRoutingProfile.FOUR_MODE
    validated, _warnings = validate_startup_configuration(
        cfg, MODES, enforce_g3_shadow=True
    )
    assert validated.mode_stances["M3_TACTICAL_POSITIONAL"] is ExecutionMode.PAPER


def test_m3_debit_spread_reaches_paper_fill_with_mode_tag(
    store: TradingStore, clock: FrozenClock
) -> None:
    """M3 debit vertical: binding → risk approval → paper fill → persisted position."""
    runner = _runner(store, clock)
    call_long = _option_snap("24000", OptionType.CALL, ask="50.00")
    call_short = _option_snap("24200", OptionType.CALL, ask="30.00")
    macro = MacroAssessment(
        regime="RISK_ON",
        directional_bias=MacroBias.BULLISH,
        confidence=Decimal("0.8"),
        fresh_until=clock.now_utc() + timedelta(hours=1),
        model_version="test",
    )
    req_m3 = PaperStrategyRequest(
        strategy_id="debit_spread",
        underlying=_underlying(),
        candidates=(call_long, call_short),
        instruments={
            call_long.contract.symbol: _spec(
                call_long.contract.symbol, "24000", OptionType.CALL
            ),
            call_short.contract.symbol: _spec(
                call_short.contract.symbol, "24200", OptionType.CALL
            ),
        },
        event_risk_state=f.event_risk_state(),
        experiment_id="EXP-M3",
        execution_mode=ExecutionMode.PAPER,
        macro=macro,
        execute=True,
        forced_mode_id=ModeId.M3_TACTICAL_POSITIONAL,
        forced_family_id=FamilyId.bull_call_debit,
    )
    result = runner.run_cycle((req_m3,))
    outcome = result.outcomes[0]
    assert outcome.intents[0].mode_id is ModeId.M3_TACTICAL_POSITIONAL
    assert outcome.decisions
    assert outcome.decisions[0].action in {RiskAction.APPROVE, RiskAction.RESIZE}
    assert outcome.order_events
    assert outcome.order_events[0].state is OrderState.FILLED


def test_restart_recovery_preserves_open_position(
    store: TradingStore, clock: FrozenClock
) -> None:
    """Restart reconstructs open book and blocks duplicate re-entry."""
    runner = _runner(store, clock)
    opt = _option_snap("24000", OptionType.CALL)
    spec = _spec(opt.contract.symbol, "24000", OptionType.CALL)
    macro = MacroAssessment(
        regime="RISK_ON",
        directional_bias=MacroBias.BULLISH,
        confidence=Decimal("0.8"),
        fresh_until=clock.now_utc() + timedelta(hours=1),
        model_version="test",
    )
    req = PaperStrategyRequest(
        strategy_id="positional_long_option",
        underlying=_underlying(),
        candidates=(opt,),
        instruments={opt.contract.symbol: spec},
        event_risk_state=f.event_risk_state(),
        experiment_id="EXP-RESTART",
        execution_mode=ExecutionMode.PAPER,
        macro=macro,
        execute=True,
        forced_mode_id=ModeId.M2_DIRECTIONAL,
        forced_family_id=FamilyId.long_call,
    )
    first = runner.run_cycle((req,))
    assert first.outcomes[0].order_events
    runner.recover_lifecycle()
    positions = runner._services.trade_manager.list_positions()
    assert positions
    assert positions[0].state in {TradeState.OPEN, TradeState.OPENING}
    dup = runner.run_cycle((req,))
    assert ReasonCode.EXACT_DUPLICATE_SUPPRESSED in dup.outcomes[0].rejection_reasons


def test_legacy_config_remains_independent() -> None:
    legacy = load_paper_session_config(ROOT / "config" / "paper_session_legacy.yaml")
    four = load_paper_session_config(ROOT / "config" / "paper_session_four_mode.yaml")
    assert legacy.routing_profile is SessionRoutingProfile.LEGACY
    assert four.routing_profile is SessionRoutingProfile.FOUR_MODE
    assert legacy.strategy_ids
    assert not four.strategy_ids
