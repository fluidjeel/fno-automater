"""Multileg structure openers shared by lifecycle tests.

Every structure is opened through the real four-mode runner so exit sequencing
and accounting are exercised on the production path, not on hand-built state.
"""

from __future__ import annotations

from typing import Any

from tests.test_four_mode_session_integration import _runner
from tests.test_four_mode_trade_simulations import _macro, _option, _request, _spec
from tests.test_p10_iron_condor_binder_and_g2 import _condor_candidates
from tests.test_p10_iron_condor_binder_and_g2 import _macro as m4_macro
from tests.test_p11_m4_broad_basket import _call_butterfly_candidates
import tests.factories as f
from trading.domain.clock import FrozenClock
from trading.domain.contracts import FeatureSnapshot
from trading.domain.enums import ExecutionMode, FamilyId, ModeId, OptionType
from trading.runtime.paper_runner import PaperStrategyRequest
from trading.storage.trading_store import TradingStore

Opened = tuple[Any, Any, dict[str, FeatureSnapshot]]


def _finish(runner: Any, candidates: tuple[FeatureSnapshot, ...]) -> Opened:
    position = runner.trade_manager.list_positions()[0]
    snapshots = {item.contract.symbol: item for item in candidates}
    return runner, position, snapshots


def _open_vertical(
    store: TradingStore,
    clock: FrozenClock,
    *,
    strategy_id: str,
    family_id: FamilyId,
    candidates: tuple[FeatureSnapshot, ...],
    bias: Any,
) -> Opened:
    runner = _runner(store, clock)
    result = runner.run_cycle(
        (
            _request(
                strategy_id=strategy_id,
                candidates=candidates,
                mode_id=ModeId.M3_TACTICAL_POSITIONAL,
                family_id=family_id,
                macro=_macro(bias),
            ),
        )
    )
    assert result.outcomes[0].order_events
    return _finish(runner, candidates)


def open_bull_put_credit(store: TradingStore, clock: FrozenClock) -> Opened:
    """Short 24000 PE protected by long 23950 PE."""
    from trading.strategies.macro import MacroBias

    short_leg = _option("24000", OptionType.PUT, ask="35.00")
    long_leg = _option("23950", OptionType.PUT, ask="15.00")
    return _open_vertical(
        store,
        clock,
        strategy_id="bull_put_credit",
        family_id=FamilyId.bull_put_credit,
        candidates=(short_leg, long_leg),
        bias=MacroBias.BULLISH,
    )


def open_bull_call_debit(store: TradingStore, clock: FrozenClock) -> Opened:
    """Long 24000 CE financed by short 24200 CE."""
    from trading.strategies.macro import MacroBias

    long_leg = _option("24000", OptionType.CALL, ask="50.00")
    short_leg = _option("24200", OptionType.CALL, ask="30.00")
    return _open_vertical(
        store,
        clock,
        strategy_id="debit_spread",
        family_id=FamilyId.bull_call_debit,
        candidates=(long_leg, short_leg),
        bias=MacroBias.BULLISH,
    )


def _open_m4(
    store: TradingStore,
    clock: FrozenClock,
    *,
    strategy_id: str,
    family_id: FamilyId,
    candidates: tuple[FeatureSnapshot, ...],
    experiment_id: str,
) -> Opened:
    runner = _runner(store, clock)
    instruments = {
        item.contract.symbol: _spec(
            item.contract.symbol,
            str(item.contract.strike or "0"),
            item.contract.option_type or OptionType.CALL,
        )
        for item in candidates
    }
    result = runner.run_cycle(
        (
            PaperStrategyRequest(
                strategy_id=strategy_id,
                underlying=f.snapshot(
                    snapshot_id="SNAP-UNDER",
                    contract=f.index_contract(),
                    market=f.quote(last=f.price("24500"), close=f.price("24500")),
                ),
                candidates=candidates,
                instruments=instruments,
                event_risk_state=f.event_risk_state(),
                experiment_id=experiment_id,
                execution_mode=ExecutionMode.PAPER,
                macro=m4_macro(),
                execute=True,
                forced_family_id=family_id,
            ),
        )
    )
    assert result.outcomes[0].order_events
    return _finish(runner, candidates)


def open_iron_condor(store: TradingStore, clock: FrozenClock) -> Opened:
    """Short strangle protected by long wings on both sides."""
    return _open_m4(
        store,
        clock,
        strategy_id="iron_condor",
        family_id=FamilyId.short_iron_condor_defined,
        candidates=_condor_candidates(),
        experiment_id="EXP-M4-IC",
    )


def open_call_butterfly(store: TradingStore, clock: FrozenClock) -> Opened:
    """Long wings around a short body."""
    return _open_m4(
        store,
        clock,
        strategy_id=FamilyId.long_call_butterfly.value,
        family_id=FamilyId.long_call_butterfly,
        candidates=_call_butterfly_candidates(),
        experiment_id="EXP-M4-LCB",
    )
