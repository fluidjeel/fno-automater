"""DISC-A6: M2 direction fallback, two-pass strike pick, binder reason wiring."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import tests.factories as f
from tests.factories import NOW
from trading.config.discovery import load_discovery_config
from trading.domain.clock import FrozenClock
from trading.domain.contracts import FeatureSnapshot, MarketState
from trading.domain.contracts.identification import (
    MacroStatus,
    TrendState,
    VolatilityState,
)
from trading.domain.contracts.snapshot import DerivativesContext, Greeks
from trading.domain.enums import (
    DataQuality,
    EntryProfile,
    OptionType,
    ReasonCode,
)
from trading.identification import bind_m2_long_option, load_identification_policy
from trading.identification.market_state import apply_discovery_direction_fallback
from trading.runtime.paper_runner import PaperRunner, PaperStrategyRequest
from trading.storage.trading_store import TradingStore
from trading.strategies import LongOptionStrategy, StrategyContext, build_strategy
from trading.strategies.macro import MacroAssessment, MacroBias

ROOT = Path(__file__).resolve().parent.parent
POLICY = load_identification_policy(ROOT / "config" / "identification.yaml")
DISCOVERY = load_discovery_config(ROOT / "config" / "discovery.yaml").config
KOLKATA = ZoneInfo("Asia/Kolkata")
CALCULATED_AT = datetime(2026, 9, 24, 10, 0, tzinfo=KOLKATA)
NOW_CTX = NOW + timedelta(seconds=60)


def _market(**overrides: object) -> MarketState:
    payload: dict[str, object] = {
        "market_state_id": "market-a6",
        "feature_version": POLICY.feature_version,
        "calculated_at": CALCULATED_AT.astimezone(UTC),
        "source_snapshot_ids": ("snapshot-1",),
        "trend": TrendState.MIXED,
        "volatility": VolatilityState.NORMAL,
        "return_15m": Decimal("0.008"),
        "return_60m": Decimal("0.015"),
        "normalized_return_15m": Decimal("0.4"),
        "normalized_return_60m": Decimal("0.5"),
        "normalized_vwap_distance": Decimal("0.3"),
        "realized_volatility_ratio": Decimal("1.0"),
        "realized_volatility_annualized": Decimal("15"),
        "iv_percentile": Decimal("30"),
        "iv_rv_ratio": Decimal("1.0"),
        "trend_score": Decimal("0.35"),
        "event_state": "NORMAL",
        "macro_status": MacroStatus.NEUTRAL,
        "quality": DataQuality.VALID,
        "warmup_complete": False,
        "completed_bar_count": 20,
        "session_count": 5,
        "reason_codes": (ReasonCode.WARMUP_INCOMPLETE,),
    }
    payload.update(overrides)
    return MarketState.model_validate(payload)


def _option(
    symbol: str,
    *,
    delta: str,
    expiry: date,
    dte: int,
    option_type: OptionType = OptionType.CALL,
) -> FeatureSnapshot:
    return f.snapshot(
        snapshot_id=f"snap-{symbol}",
        contract=f.option_contract(
            symbol=symbol,
            expiry=expiry,
            strike=Decimal("24000"),
            option_type=option_type,
        ),
        market=f.quote(
            bid=f.price("100"),
            ask=f.price("101"),
            last=f.price("100.50"),
            volume=5000,
        ),
        derivatives=DerivativesContext(
            days_to_expiry=dte,
            open_interest=5000,
            option_type=option_type,
            greeks=Greeks(
                model="fixture",
                calculation_version="1",
                converged=True,
                implied_volatility=Decimal("15"),
                delta=Decimal(delta),
            ),
            underlying_price=f.price("24000"),
        ),
        features={
            "lot_size": Decimal(75),
            "top_of_book_observed": Decimal(1),
        },
    )


def test_discovery_direction_fallback_binds_call_strict_abstains() -> None:
    """MIXED strict trend with aligned returns: DISCOVERY binds, STRICT abstains."""
    strict_market = _market()
    assert strict_market.trend is TrendState.MIXED

    discovery_market = apply_discovery_direction_fallback(
        strict_market,
        direction=DISCOVERY.direction,
        entry_profile=EntryProfile.DISCOVERY,
    )
    assert discovery_market.trend is TrendState.UP
    assert ReasonCode.DIRECTION_FALLBACK in discovery_market.reason_codes
    assert ReasonCode.WARMUP_INCOMPLETE in discovery_market.reason_codes

    chain = (
        _option(
            "CALL_FW",
            delta="0.52",
            expiry=date(2026, 10, 1),
            dte=7,
        ),
    )
    bound_discovery = bind_m2_long_option(
        chain,
        market=discovery_market,
        policy=POLICY,
        allow_fallback_expiry=True,
        discovery_config=DISCOVERY,
        entry_profile=EntryProfile.DISCOVERY,
    )
    assert bound_discovery.binding.eligible is True
    assert bound_discovery.binding.selected_symbols == ("CALL_FW",)
    assert ReasonCode.DIRECTION_FALLBACK in bound_discovery.binding.reason_codes

    bound_strict = bind_m2_long_option(
        chain,
        market=strict_market,
        policy=POLICY,
        allow_fallback_expiry=True,
        discovery_config=DISCOVERY,
        entry_profile=EntryProfile.STRICT,
    )
    assert bound_strict.binding.eligible is False
    assert bound_strict.binding.reason_codes == (ReasonCode.DIRECTION_UNRESOLVED,)
    assert bound_strict.candidates == ()


def test_discovery_delta_fallback_picks_closest_relaxed_delta() -> None:
    """Only 0.38 and 0.72 available: DISCOVERY picks 0.38 tagged M2_DELTA_FALLBACK."""
    market = _market(trend=TrendState.UP, warmup_complete=True, reason_codes=())
    low_delta = _option(
        "CALL_038",
        delta="0.38",
        expiry=date(2026, 10, 1),
        dte=7,
    )
    high_delta = _option(
        "CALL_072",
        delta="0.72",
        expiry=date(2026, 10, 1),
        dte=7,
    )
    bound = bind_m2_long_option(
        (low_delta, high_delta),
        market=market,
        policy=POLICY,
        allow_fallback_expiry=True,
        discovery_config=DISCOVERY,
        entry_profile=EntryProfile.DISCOVERY,
    )
    assert bound.binding.eligible is True
    assert bound.binding.selected_symbols == ("CALL_038",)
    assert ReasonCode.M2_DELTA_FALLBACK in bound.binding.reason_codes
    assert bound.setup_features is not None
    assert bound.setup_features.score_components["M2_DELTA_FALLBACK"] == Decimal("0.38")

    bound_strict = bind_m2_long_option(
        (low_delta, high_delta),
        market=market,
        policy=POLICY,
        allow_fallback_expiry=True,
        discovery_config=DISCOVERY,
        entry_profile=EntryProfile.STRICT,
    )
    assert bound_strict.binding.eligible is False
    assert bound_strict.binding.reason_codes != ()


def test_discovery_expiry_fallback_and_hard_1_dte_reject() -> None:
    """3 DTE fallback binds under DISCOVERY; 1 DTE is a hard reject."""
    market = _market(trend=TrendState.UP, warmup_complete=True, reason_codes=())
    dte3 = _option(
        "CALL_3DTE",
        delta="0.52",
        expiry=date(2026, 9, 27),
        dte=3,
    )
    bound = bind_m2_long_option(
        (dte3,),
        market=market,
        policy=POLICY,
        allow_fallback_expiry=True,
        discovery_config=DISCOVERY,
        entry_profile=EntryProfile.DISCOVERY,
    )
    assert bound.binding.eligible is True
    assert bound.setup_features is not None
    assert bound.setup_features.score_components["m2_expiry_fallback"] == Decimal(1)

    dte1 = _option(
        "CALL_1DTE",
        delta="0.52",
        expiry=date(2026, 9, 25),
        dte=1,
    )
    bound_1 = bind_m2_long_option(
        (dte1,),
        market=market,
        policy=POLICY,
        allow_fallback_expiry=True,
        discovery_config=DISCOVERY,
        entry_profile=EntryProfile.DISCOVERY,
    )
    assert bound_1.binding.eligible is False
    assert bound_1.binding.reason_codes == (ReasonCode.EXPIRY_0_1_DTE_EXCLUDED,)


def test_paper_runner_persists_binder_reason_when_candidates_empty(
    tmp_path: Path,
) -> None:
    """Empty bind must surface binder reason codes, not INSTRUMENT_UNKNOWN."""
    from tests.test_paper_runner import BROKER_FIXTURES, _paper_config
    from trading.broker.paper import PaperBroker
    from trading.config import load_risk_policy
    from trading.domain.ids import SequentialIdFactory

    clock = FrozenClock(NOW_CTX)
    store = TradingStore.open(tmp_path / "disc_a6.sqlite", clock=clock)
    try:
        ids = SequentialIdFactory(clock.instant)
        broker = PaperBroker.from_fixtures(BROKER_FIXTURES, clock=clock, id_factory=ids)
        runner = PaperRunner(
            account_config=_paper_config(),  # type: ignore[arg-type]
            risk_policy=load_risk_policy(ROOT / "config" / "risk.yaml"),
            store=store,
            broker=broker,
            clock=clock,
            id_factory=ids,
        )
        request = PaperStrategyRequest(
            strategy_id="positional_long_option",
            underlying=f.snapshot(
                snapshot_id="SNAP-UNDER",
                contract=f.index_contract(),
                market=f.quote(last=f.price("24150"), close=f.price("24000")),
            ),
            candidates=(),
            instruments={},
            event_risk_state=f.event_risk_state(),
            experiment_id="EXP-DISC-A6",
            binding_reason_codes=(ReasonCode.DIRECTION_UNRESOLVED,),
        )
        result = runner.run_cycle((request,))
        outcome = result.outcomes[0]
        assert outcome.rejection_reasons == (ReasonCode.DIRECTION_UNRESOLVED,)
        assert ReasonCode.INSTRUMENT_UNKNOWN not in outcome.rejection_reasons
    finally:
        store.close()


def test_long_option_silent_abstains_emit_reason_codes() -> None:
    """Neutral direction and option-type mismatch must not stay silent."""
    strategy = build_strategy("positional_long_option")
    neutral_ctx = StrategyContext(
        underlying=f.snapshot(
            snapshot_id="SNAP-UNDER",
            contract=f.index_contract(),
            market=f.quote(last=f.price("24000"), close=f.price("24000")),
        ),
        candidates=(
            _option(
                "CALL_OPT",
                delta="0.52",
                expiry=date(2026, 10, 1),
                dte=7,
            ),
        ),
        view=f.portfolio_view(),
        now=NOW_CTX,
        experiment_id="EXP-DISC-A6",
    )
    neutral = strategy.evaluate(neutral_ctx)
    assert not neutral.intents
    assert neutral.rejections[0].reason is ReasonCode.DIRECTION_NEUTRAL

    mismatch_ctx = StrategyContext(
        underlying=f.snapshot(
            snapshot_id="SNAP-UNDER",
            contract=f.index_contract(),
            market=f.quote(last=f.price("24100"), close=f.price("24000")),
        ),
        candidates=(
            _option(
                "PUT_OPT",
                delta="-0.52",
                expiry=date(2026, 10, 1),
                dte=7,
                option_type=OptionType.PUT,
            ),
        ),
        view=f.portfolio_view(),
        now=NOW_CTX,
        experiment_id="EXP-DISC-A6",
        macro=MacroAssessment(
            regime="RISK_ON",
            directional_bias=MacroBias.BULLISH,
            confidence=Decimal("0.8"),
            fresh_until=NOW_CTX + timedelta(hours=1),
            model_version="test",
        ),
    )
    mismatch = LongOptionStrategy().evaluate(mismatch_ctx)
    assert not mismatch.intents
    assert mismatch.rejections[0].reason is ReasonCode.OPTION_TYPE_MISMATCH
