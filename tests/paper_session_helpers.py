"""Shared paper-runner fixtures for M2 integration and audit tests."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import cast

import tests.factories as f
from tests.test_paper_runner import BROKER_FIXTURES, ROOT, _paper_config
from trading.broker.paper import PaperBroker
from trading.broker.ports import MarginPreviewRequest, MarginPreviewResult
from trading.config import LoadedConfig, load_evaluation_config, load_risk_policy
from trading.domain.clock import FrozenClock
from trading.domain.contracts import DerivativesContext, FeatureSnapshot, InstrumentSpec
from trading.domain.contracts.mode_policy import load_modes_config
from trading.domain.enums import Exchange, ExecutionMode, FamilyId, InstrumentKind, ModeId, OptionType
from trading.domain.ids import SequentialIdFactory
from trading.news.contracts import EventRiskState
from trading.risk import CapitalReservationService, RiskGateway
from trading.runtime.paper_runner import PaperRunner, PaperStrategyRequest
from trading.storage.trading_store import TradingStore
from trading.strategies.macro import MacroAssessment, MacroBias

NOW = f.NOW
MODES = load_modes_config(ROOT / "config" / "modes.yaml")


class _ConfirmedMarginPreview:
    def preview_margin(self, request: MarginPreviewRequest) -> MarginPreviewResult:
        return MarginPreviewResult(
            request_id=request.request_id,
            as_of=NOW,
            margin_required=f.money("5000"),
            margin_available_after=f.money("5000000"),
            confirmed=True,
        )


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


def _runner(store: TradingStore, clock: FrozenClock) -> PaperRunner:
    from zoneinfo import ZoneInfo

    from trading.risk.mode_ledger import FourModeBook

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


def _macro(bias: MacroBias) -> MacroAssessment:
    return MacroAssessment(
        regime="RISK_ON",
        directional_bias=bias,
        confidence=Decimal("0.8"),
        fresh_until=f.NOW + timedelta(hours=1),
        model_version="test",
    )


def _option(
    strike: str, option_type: OptionType, ask: str = "50.00"
) -> FeatureSnapshot:
    return _option_snap(strike, option_type, ask=ask)


def _request(
    *,
    strategy_id: str,
    candidates: tuple[FeatureSnapshot, ...],
    mode_id: ModeId,
    family_id: FamilyId,
    macro: MacroAssessment,
    underlying: FeatureSnapshot | None = None,
    event_risk_state: EventRiskState | None = None,
) -> PaperStrategyRequest:
    instruments = {
        c.contract.symbol: _spec(
            c.contract.symbol,
            str(c.contract.strike or "0"),
            c.contract.option_type or OptionType.CALL,
        )
        for c in candidates
    }
    return PaperStrategyRequest(
        strategy_id=strategy_id,
        underlying=underlying or _underlying(),
        candidates=candidates,
        instruments=instruments,
        event_risk_state=event_risk_state or f.event_risk_state(),
        experiment_id=f"EXP-{mode_id.value}-{family_id.value}",
        execution_mode=ExecutionMode.PAPER,
        macro=macro,
        execute=True,
        forced_mode_id=mode_id,
        forced_family_id=family_id,
    )
