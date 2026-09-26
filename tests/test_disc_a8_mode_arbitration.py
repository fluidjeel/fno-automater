"""DISC-A8: within-mode duplicates; daily entry caps; arbiter reason persistence."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import cast

import tests.factories as f
from tests.test_paper_runner import BROKER_FIXTURES, ROOT, _paper_config
from tests.test_p12_economic_overlap_m4_cap import (
    NOW,
    _bull_call_debit,
    _bull_put_credit,
    _option_snap,
)
from trading.broker.paper import PaperBroker
from trading.config import load_evaluation_config, load_risk_policy
from trading.config.discovery import load_discovery_config
from trading.domain.clock import FrozenClock
from trading.domain.contracts import (
    FeatureSnapshot,
    IntentLeg,
    PositionState,
    TradeIntent,
)
from trading.domain.enums import (
    EntryProfile,
    FamilyId,
    ModeId,
    OptionType,
    ReasonCode,
    Side,
    TradeState,
)
from trading.domain.ids import SequentialIdFactory
from trading.domain.primitives import Currency, Money
from trading.portfolio.arbitration import PortfolioArbiter, count_mode_entries_today
from trading.runtime.paper_runner import PaperRunner
from trading.storage.trading_store import TradingStore

DISCOVERY = load_discovery_config(ROOT / "config" / "discovery.yaml")


def _discovery_arbiter() -> PortfolioArbiter:
    return PortfolioArbiter(
        max_m4_open_positions=4,
        discovery_config=DISCOVERY,
        entry_profile=EntryProfile.DISCOVERY,
    )


def _strict_arbiter() -> PortfolioArbiter:
    return PortfolioArbiter(max_m4_open_positions=2, entry_profile=EntryProfile.STRICT)


def _call_snap(symbol: str, strike: str) -> FeatureSnapshot:
    return cast(FeatureSnapshot, _option_snap(symbol, strike, OptionType.CALL))


def _m2_long_call(intent_id: str = "INTENT-M2-CALL") -> TradeIntent:
    snap = _call_snap("NIFTY26OCT25000CE", "25000")
    return f.intent(
        intent_id=intent_id,
        mode_id=ModeId.M2_DIRECTIONAL,
        family_id=FamilyId.long_call.value,
        legs=(
            IntentLeg(
                leg_id="leg-call",
                contract=snap.contract,
                side=Side.BUY,
                ratio=1,
            ),
        ),
        strategy_confidence=Decimal("0.85"),
        requested_risk=Money.of("3000", Currency.INR),
        estimated_max_loss=Money.of("3000", Currency.INR),
        created_at=NOW,
    )


def _m3_bull_call_spread(intent_id: str = "INTENT-M3-SPREAD") -> TradeIntent:
    low = _call_snap("NIFTY26OCT25000CE", "25000")
    high = _call_snap("NIFTY26OCT25200CE", "25200")
    return f.intent(
        intent_id=intent_id,
        mode_id=ModeId.M3_TACTICAL_POSITIONAL,
        family_id=FamilyId.bull_call_debit.value,
        legs=(
            IntentLeg(leg_id="leg-low", contract=low.contract, side=Side.BUY, ratio=1),
            IntentLeg(
                leg_id="leg-high", contract=high.contract, side=Side.SELL, ratio=1
            ),
        ),
        strategy_confidence=Decimal("0.80"),
        requested_risk=Money.of("2000", Currency.INR),
        estimated_max_loss=Money.of("2000", Currency.INR),
        created_at=NOW,
    )


def _open_m2_call_position(trade_id: str) -> PositionState:
    snap = _call_snap("NIFTY26OCT25000CE", "25000")
    return f.position_state(
        trade_id=trade_id,
        intent_id=f"{trade_id}-INT",
        mode_id=ModeId.M2_DIRECTIONAL,
        state=TradeState.OPEN,
        legs=(
            f.position_leg_state(
                leg_id="leg-call",
                contract=snap.contract,
                side=Side.BUY,
            ),
        ),
        exit_policy=f.exit_policy(trade_id=trade_id),
        opened_at=NOW,
    )


class TestDiscA8CrossModeDuplicateAllowed:
    def test_m3_spread_allowed_while_m2_same_strike_open(self) -> None:
        """M2 long 25000 CE open: M3 bull call spread with same long leg is allowed."""
        arbiter = _discovery_arbiter()
        existing = (_open_m2_call_position("TRD-M2-1"),)
        m3 = _m3_bull_call_spread()
        m2_reentry = _m2_long_call("INTENT-M2-REENTRY")
        result = arbiter.arbitrate(
            [m3, m2_reentry],
            existing_positions=existing,
            now=NOW,
        )
        approved_ids = {intent.intent_id for intent in result.approved_intents}
        assert "INTENT-M3-SPREAD" in approved_ids
        assert "INTENT-M2-REENTRY" not in approved_ids
        assert len(result.suppressed_intents) == 1
        assert (
            result.suppressed_intents[0].reason_code
            is ReasonCode.EXACT_DUPLICATE_SUPPRESSED
        )
        assert result.suppressed_intents[0].candidate_intent_id == "INTENT-M2-REENTRY"


class TestDiscA8DailyEntryCap:
    def test_m2_fifth_entry_rejected_m3_unaffected(self) -> None:
        """M2 at daily cap rejects a 5th entry; M3 remains able to trade."""
        arbiter = _discovery_arbiter()
        mode_daily_entries = {ModeId.M2_DIRECTIONAL: 4}
        fifth_m2 = _m2_long_call("INTENT-M2-5")
        m3 = _m3_bull_call_spread("INTENT-M3-OK")
        result = arbiter.arbitrate(
            [fifth_m2, m3],
            now=NOW,
            mode_daily_entries=mode_daily_entries,
        )
        assert {intent.intent_id for intent in result.approved_intents} == {
            "INTENT-M3-OK"
        }
        assert len(result.suppressed_intents) == 1
        assert result.suppressed_intents[0].reason_code is ReasonCode.DAILY_ENTRY_CAP
        assert result.suppressed_intents[0].candidate_mode_id is ModeId.M2_DIRECTIONAL

    def test_count_mode_entries_today_from_lifecycle(self, tmp_path: Path) -> None:
        clock = FrozenClock(NOW)
        store = TradingStore.open(tmp_path / "a8.sqlite", clock=clock)
        session_date = NOW.astimezone().date()
        for idx in range(4):
            record = f.position_lifecycle_record(
                trade_id=f"TRD-M2-{idx}",
                mode_id=ModeId.M2_DIRECTIONAL,
                position=f.position_state(
                    trade_id=f"TRD-M2-{idx}",
                    intent_id=f"INT-M2-{idx}",
                    mode_id=ModeId.M2_DIRECTIONAL,
                    opened_at=NOW,
                    exit_policy=f.exit_policy(trade_id=f"TRD-M2-{idx}"),
                ),
            )
            store.upsert_position_lifecycle(record, event_id=f"EVT-M2-{idx}")
        counts = count_mode_entries_today(
            store.list_position_lifecycle(),
            session_date=session_date,
        )
        assert counts[ModeId.M2_DIRECTIONAL] == 4
        store.close()


class TestDiscA8OverlapReasonPersistence:
    def test_arbiter_records_economic_overlap_code(self) -> None:
        """STRICT overlap suppression uses ECONOMIC_OVERLAP_SUPPRESSED."""
        arbiter = _strict_arbiter()
        incumbent = cast(TradeIntent, _bull_call_debit("INTENT-BULL-CALL"))
        challenger = cast(TradeIntent, _bull_put_credit("INTENT-BULL-PUT"))
        result = arbiter.arbitrate([incumbent, challenger], now=NOW)
        assert (
            result.suppressed_intents[0].reason_code
            is ReasonCode.ECONOMIC_OVERLAP_SUPPRESSED
        )

    def test_paper_runner_suppression_mapping_uses_arbiter_reason(
        self, tmp_path: Path
    ) -> None:
        """STRICT runner arbiter overlap maps to ECONOMIC_OVERLAP_SUPPRESSED."""
        clock = FrozenClock(NOW)
        store = TradingStore.open(tmp_path / "a8_overlap.sqlite", clock=clock)
        ids = SequentialIdFactory(clock.instant)
        fill_model = load_evaluation_config(
            ROOT / "config" / "evaluation.yaml"
        ).config.fill_model
        broker = PaperBroker.from_fixtures(
            BROKER_FIXTURES, clock=clock, id_factory=ids, fill_model=fill_model
        )
        runner = PaperRunner(
            account_config=_paper_config(),  # type: ignore[arg-type]
            risk_policy=load_risk_policy(ROOT / "config" / "risk.yaml"),
            store=store,
            broker=broker,
            clock=clock,
            id_factory=ids,
            fill_model=fill_model,
        )
        incumbent = cast(TradeIntent, _bull_call_debit("INTENT-BULL-CALL"))
        challenger = cast(TradeIntent, _bull_put_credit("INTENT-BULL-PUT"))
        arb = runner._arbiter.arbitrate([incumbent, challenger], now=NOW)
        suppressions = {s.candidate_intent_id: s for s in arb.suppressed_intents}
        suppression = suppressions.get("INTENT-BULL-PUT")
        reason = (
            suppression.reason_code
            if suppression is not None
            else ReasonCode.EXACT_DUPLICATE_SUPPRESSED
        )
        assert reason is ReasonCode.ECONOMIC_OVERLAP_SUPPRESSED
        store.close()


class TestDiscA8DiscoverySoftOverlap:
    def test_discovery_approves_overlap_with_soft_warning(self) -> None:
        """DISCOVERY allows economic overlap and records a strict_would_block shadow."""
        arbiter = _discovery_arbiter()
        incumbent = cast(TradeIntent, _bull_call_debit("INTENT-BULL-CALL"))
        challenger = cast(TradeIntent, _bull_put_credit("INTENT-BULL-PUT"))
        result = arbiter.arbitrate([incumbent, challenger], now=NOW)
        assert len(result.approved_intents) == 2
        assert len(result.suppressed_intents) == 0
        assert len(result.soft_warnings) == 1
        assert (
            result.soft_warnings[0].reason_code
            is ReasonCode.ECONOMIC_OVERLAP_SUPPRESSED
        )


class TestDiscA8RouterCooldownSoft:
    def test_discovery_router_cooldown_is_soft(self) -> None:
        from tests.test_identification import POLICY, _bound, _market
        from trading.identification.router import route_nifty_options

        route, opportunities = route_nifty_options(
            _market(),
            long_option=_bound("positional_long_option", "0.90"),
            debit_spread=_bound("debit_spread", "0.70"),
            policy=POLICY,
            cooldown_active=True,
            entry_profile=EntryProfile.DISCOVERY,
            discovery_config=DISCOVERY,
        )
        assert route.paper_winner == "positional_long_option"
        assert ReasonCode.SETUP_COOLDOWN in route.strict_would_block
        assert ReasonCode.SETUP_COOLDOWN not in route.reason_codes
        assert sum(item.execution for item in opportunities) == 1
