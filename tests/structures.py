"""Structure openers shared by lifecycle and fill-charge tests."""

from __future__ import annotations

from typing import Any

import tests.factories as f
from tests.paper_session_helpers import _macro, _option, _request, _runner
from trading.domain.clock import FrozenClock
from trading.domain.contracts import FeatureSnapshot
from trading.domain.enums import FamilyId, ModeId, OptionType
from trading.storage.trading_store import TradingStore
from trading.strategies.macro import MacroBias

Opened = tuple[Any, Any, dict[str, FeatureSnapshot]]


def _finish(runner: Any, candidates: tuple[FeatureSnapshot, ...]) -> Opened:
    position = runner.trade_manager.list_positions()[0]
    snapshots = {item.contract.symbol: item for item in candidates}
    return runner, position, snapshots


def open_m2_long_call(store: TradingStore, clock: FrozenClock) -> Opened:
    """M2 directional long call through the paper runner."""
    opt = _option("24000", OptionType.CALL)
    runner = _runner(store, clock)
    result = runner.run_cycle(
        (
            _request(
                strategy_id="positional_long_option",
                candidates=(opt,),
                mode_id=ModeId.M2_DIRECTIONAL,
                family_id=FamilyId.long_call,
                macro=_macro(MacroBias.BULLISH),
            ),
        )
    )
    assert result.outcomes[0].order_events
    return _finish(runner, (opt,))


open_bull_call_debit = open_m2_long_call
open_bull_put_credit = open_m2_long_call
open_iron_condor = open_m2_long_call
open_call_butterfly = open_m2_long_call
