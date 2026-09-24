"""Audit remediation regressions (four_mode_20260925 + m3_m4_20260925).

Each test names the audit finding it closes. Every test here must fail on the
pre-fix tree and pass after, exercising production classes rather than
reimplementing their logic.

Invariant 8: open positions keep deterministic protection without AI.
Invariant 14: capital is reserved before submission and released from
confirmed events only.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import tests.factories as f
from tests.test_paper_lifecycle import _option_snapshot, _Sink
from tests.test_paper_review import (
    SESSION_DATE,
    _nse_slots,
    _open_reviewable_long,
)
from tests.test_paper_runner import ROOT
from trading.domain.clock import FrozenClock
from trading.domain.contracts import DerivativesContext, FeatureSnapshot
from trading.domain.contracts.common import ContractRef
from trading.domain.enums import ModeId, OptionType, ReasonCode, Side, TradeState
from trading.risk.mode_ledger import FourModeBook
from trading.trade.exits import ExitEvaluation, ExitKind
from trading.domain.primitives import Currency, Money
from trading.runtime.paper_runner import PaperRunner
from trading.runtime.paper_session import PaperSession, load_paper_session_config
from trading.storage.trading_store import TradingStore

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 9, 14, 4, 0, tzinfo=UTC)
SLOT_1030 = datetime(2026, 9, 14, 5, 0, tzinfo=UTC)


def _review_snapshot(
    position_contract: ContractRef,
    *,
    at: datetime = NOW,
    days_to_expiry: int = 10,
    bid: str = "52.00",
    ask: str = "52.05",
) -> FeatureSnapshot:
    """Quote that is fresh at ``at`` so the scheduled review actually runs.

    The default price sits between the frozen stop (48.00) and target, so the
    continuous exit engine holds and the scheduled review is reached.
    """
    return _option_snapshot(
        position_contract,
        market=f.quote(bid=f.price(bid), ask=f.price(ask)),
        derivatives=DerivativesContext(
            days_to_expiry=days_to_expiry,
            open_interest=5000,
            option_type=OptionType.CALL,
            underlying_price=f.price("24000"),
        ),
        times=f.snapshot_times(
            event_time=at,
            source_time=at,
            receive_time=at + timedelta(milliseconds=50),
            calculation_time=at + timedelta(milliseconds=120),
        ),
    )


def _review_session(
    runner: PaperRunner,
    clock: FrozenClock,
    tmp_path: Path,
    sink: _Sink,
    snapshots: dict[str, FeatureSnapshot],
) -> PaperSession:
    """Real PaperSession whose builder supplies only exit/review snapshots."""
    session_cfg = load_paper_session_config(ROOT / "config" / "paper_session.yaml")

    def builder(
        _now: datetime,
    ) -> tuple[tuple[object, ...], dict[str, FeatureSnapshot]]:
        return ((), snapshots)

    return PaperSession(
        runner=runner,
        clock=clock,
        session_config=session_cfg,
        session_hours=(time(9, 15), time(15, 30)),
        timezone=IST,
        notifier=sink,
        request_builder=builder,  # type: ignore[arg-type]
        observation_start=NOW,
        capital_limit=Money.of("700000", Currency.INR),
        risk_policy_version="6",
        fill_model_version="conservative-v1",
        code_version="audit-remediation",
        charges_verified=False,
        cohort_dir=tmp_path / "cohorts",
    )


def _raise_timeout(calls: list[str]):
    def _shadow(*_args: object, **_kwargs: object) -> None:
        calls.append("shadow")
        raise TimeoutError("SIMULATED AI timeout")

    return _shadow


class TestP0AiCannotBlockDeterministicExit:
    """four_mode_20260925 §8.8 / m3_m4_20260925 P1: AI timeout blocked an exit."""

    def test_session_tick_survives_shadow_timeout_and_keeps_protection(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Invariant 8: a shadow timeout must not abort the production tick.

        Runs the real session path (PaperSession.tick -> _run_due_reviews ->
        PaperRunner.run_review_slot). Before the fix the TimeoutError escaped
        tick(), skipping the remaining exit/carry/heartbeat work for that cycle.
        """
        clock = FrozenClock(NOW + timedelta(seconds=60))
        store = TradingStore.open(tmp_path / "session-ai-timeout.sqlite", clock=clock)
        try:
            runner = _open_reviewable_long(store, clock)
            opened = runner.trade_manager.list_positions()[0]
            contract = opened.legs[0].contract
            # Healthy, in-the-money quote: the continuous exit engine holds, so
            # the scheduled review (and its shadow call) is actually reached.
            snapshot = _review_snapshot(contract, at=SLOT_1030, days_to_expiry=10)
            sink = _Sink()
            session = _review_session(
                runner, clock, tmp_path, sink, {contract.symbol: snapshot}
            )

            calls: list[str] = []
            monkeypatch.setattr(
                "trading.runtime.paper_runner.maybe_log_position_shadow",
                _raise_timeout(calls),
            )

            clock.set(SLOT_1030)
            session.tick()

            # The failing shadow must actually have been reached, otherwise this
            # test would pass without exercising the AI coupling at all.
            assert calls, "review path did not invoke shadow logging"
            # The slot is recorded and the position keeps deterministic cover.
            assert store.has_review_slot_run(_nse_slots()[0].slot_id, SESSION_DATE)
            still = runner.trade_manager.get_position(opened.trade_id)
            assert still is not None
            assert still.state is TradeState.OPEN
            runner.flush_lifecycle()
            reviewed = [
                record for record in store.list_position_lifecycle() if record.reviews
            ]
            assert reviewed, "review row must persist despite shadow failure"
        finally:
            store.close()

    def test_review_full_exit_submits_despite_shadow_timeout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The audit's own scenario: a review FULL_EXIT under AI timeout.

        Before the fix the TimeoutError propagated out of run_review_slot, the
        position stayed OPEN and no SELL was ever sent.
        """
        clock = FrozenClock(NOW + timedelta(seconds=60))
        store = TradingStore.open(tmp_path / "review-ai-exit.sqlite", clock=clock)
        try:
            runner = _open_reviewable_long(store, clock)
            opened = runner.trade_manager.list_positions()[0]
            contract = opened.legs[0].contract
            snapshot = _review_snapshot(
                contract, days_to_expiry=1, bid="91.95", ask="92.00"
            )

            calls: list[str] = []
            monkeypatch.setattr(
                "trading.runtime.paper_runner.maybe_log_position_shadow",
                _raise_timeout(calls),
            )

            result = runner.run_review_slot(
                _nse_slots()[0],
                {contract.symbol: snapshot},
                session_date=SESSION_DATE,
                configured_slots=_nse_slots(),
            )

            assert calls, "review path did not invoke shadow logging"
            assert result.decisions
            closed = runner.trade_manager.get_position(opened.trade_id)
            assert closed is not None
            assert closed.state is TradeState.CLOSED
            sells = [
                event
                for event in runner.broker.list_orders()
                if event.command.side.value == "SELL"
            ]
            assert sells, "deterministic exit must submit even when AI logging fails"
        finally:
            store.close()


def _remaining_after(
    position: object, executed: tuple[object, ...]
) -> dict[str, dict[str, int]]:
    """Contracts still held per option type after the given exit orders fill."""
    held: dict[str, dict[str, int]] = {}
    for leg in position.legs:  # type: ignore[attr-defined]
        kind = (leg.contract.option_type or OptionType.CALL).value
        bucket = held.setdefault(kind, {"long": 0, "short": 0})
        side = "long" if leg.side is Side.BUY else "short"
        bucket[side] += leg.quantity_contracts
    for order in executed:
        contract = order.command.contract  # type: ignore[attr-defined]
        kind = (contract.option_type or OptionType.CALL).value
        bucket = held.setdefault(kind, {"long": 0, "short": 0})
        # A BUY exit closes a short; a SELL exit closes a long.
        side = "short" if order.command.side is Side.BUY else "long"  # type: ignore[attr-defined]
        bucket[side] -= order.command.quantity_contracts  # type: ignore[attr-defined]
    return held


def _assert_every_prefix_is_covered(position: object, plan: object) -> None:
    """No reachable intermediate state may hold an uncovered short.

    Each exit order is a separate submission, so every prefix of the plan is a
    position the account can be left holding after a delay, reject, unknown
    response or partial fill. For a defined-risk structure the remaining short
    contracts must never exceed the remaining long contracts of the same type.
    """
    orders = plan.orders  # type: ignore[attr-defined]
    for cut in range(len(orders) + 1):
        remaining = _remaining_after(position, tuple(orders[:cut]))
        for kind, bucket in remaining.items():
            assert bucket["short"] <= bucket["long"], (
                f"prefix of {cut} exit order(s) leaves an uncovered {kind} short: "
                f"{bucket}"
            )


class TestP0LiabilityFirstExitSequencing:
    """m3_m4_20260925 P0: the runner sold protection before covering shorts."""

    @staticmethod
    def _plan_for(runner: object, position: object, snapshots: dict[str, object]):
        intent, decision = runner._open_book[position.trade_id]  # type: ignore[attr-defined]
        plan = runner._exit_plan(intent, decision, position, snapshots)  # type: ignore[attr-defined]
        assert plan is not None
        return plan

    def _check(
        self, runner: object, position: object, snapshots: dict[str, object]
    ) -> None:
        plan = self._plan_for(runner, position, snapshots)
        sides = [order.command.side for order in plan.orders]
        # Every covering BUY precedes every protection-releasing SELL.
        if Side.BUY in sides and Side.SELL in sides:
            assert sides.index(Side.SELL) > max(
                index for index, side in enumerate(sides) if side is Side.BUY
            )
        _assert_every_prefix_is_covered(position, plan)

    def test_credit_spread_exit_covers_short_first(self, tmp_path: Path) -> None:
        from tests.structures import open_bull_put_credit

        clock = FrozenClock(NOW + timedelta(seconds=60))
        store = TradingStore.open(tmp_path / "credit.sqlite", clock=clock)
        try:
            runner, position, snapshots = open_bull_put_credit(store, clock)
            self._check(runner, position, snapshots)
        finally:
            store.close()

    def test_debit_spread_exit_covers_short_first(self, tmp_path: Path) -> None:
        from tests.structures import open_bull_call_debit

        clock = FrozenClock(NOW + timedelta(seconds=60))
        store = TradingStore.open(tmp_path / "debit.sqlite", clock=clock)
        try:
            runner, position, snapshots = open_bull_call_debit(store, clock)
            self._check(runner, position, snapshots)
        finally:
            store.close()

    def test_iron_condor_exit_covers_shorts_first(self, tmp_path: Path) -> None:
        from tests.structures import open_iron_condor

        clock = FrozenClock(NOW + timedelta(seconds=60))
        store = TradingStore.open(tmp_path / "condor.sqlite", clock=clock)
        try:
            runner, position, snapshots = open_iron_condor(store, clock)
            self._check(runner, position, snapshots)
        finally:
            store.close()

    def test_call_butterfly_exit_covers_shorts_first(self, tmp_path: Path) -> None:
        from tests.structures import open_call_butterfly

        clock = FrozenClock(NOW + timedelta(seconds=60))
        store = TradingStore.open(tmp_path / "butterfly.sqlite", clock=clock)
        try:
            runner, position, snapshots = open_call_butterfly(store, clock)
            self._check(runner, position, snapshots)
        finally:
            store.close()


class TestP0FailedProtectionNeverLeavesAnUncoveredShort:
    """A rejected protective leg must stop the entry, not produce a naked short.

    Found while proving the exit sequencing above: the butterfly and condor
    entries submitted the short body after both long wings were rejected, and
    the resulting position was left OPEN as a naked short.
    """

    def test_rejected_long_wings_stop_the_short_body(self, tmp_path: Path) -> None:
        from tests.test_four_mode_session_integration import _runner
        from tests.test_four_mode_trade_simulations import _spec
        from tests.test_p10_iron_condor_binder_and_g2 import _macro as m4_macro
        from tests.test_p11_m4_broad_basket import _call_butterfly_candidates
        from trading.domain.enums import ExecutionMode, FamilyId, OrderState
        from trading.runtime.paper_runner import PaperStrategyRequest

        clock = FrozenClock(f.NOW + timedelta(seconds=60))
        store = TradingStore.open(tmp_path / "wings.sqlite", clock=clock)
        try:
            # Strip the traded price so the conservative fill model cannot fill
            # the long wings, reproducing the rejected-protection case.
            candidates = tuple(
                item.model_copy(
                    update={"market": item.market.model_copy(update={"last": None})}
                )
                for item in _call_butterfly_candidates()
            )
            runner = _runner(store, clock)
            result = runner.run_cycle(
                (
                    PaperStrategyRequest(
                        strategy_id=FamilyId.long_call_butterfly.value,
                        underlying=f.snapshot(
                            snapshot_id="SNAP-UNDER",
                            contract=f.index_contract(),
                            market=f.quote(
                                last=f.price("24500"), close=f.price("24500")
                            ),
                        ),
                        candidates=candidates,
                        instruments={
                            item.contract.symbol: _spec(
                                item.contract.symbol,
                                str(item.contract.strike or "0"),
                                OptionType.CALL,
                            )
                            for item in candidates
                        },
                        event_risk_state=f.event_risk_state(),
                        experiment_id="EXP-WINGS",
                        execution_mode=ExecutionMode.PAPER,
                        macro=m4_macro(),
                        execute=True,
                        forced_family_id=FamilyId.long_call_butterfly,
                    ),
                )
            )
            events = result.outcomes[0].order_events
            assert events, "the entry must at least attempt the protective leg"
            assert events[0].command.side is Side.BUY
            assert events[0].state is OrderState.REJECTED
            # Nothing is sold once protection fails.
            assert not any(
                event.command.side is Side.SELL
                and event.state in {OrderState.FILLED, OrderState.PARTIAL}
                for event in events
            )
            for position in runner.trade_manager.list_positions():
                remaining = _remaining_after(position, ())
                for kind, bucket in remaining.items():
                    assert bucket["short"] <= bucket["long"], (
                        f"entry left an uncovered {kind} short: {bucket}"
                    )
        finally:
            store.close()


def _close_open_position(
    runner: PaperRunner,
    position: object,
    snapshots: dict[str, object],
) -> tuple[tuple[object, ...], object]:
    intent, decision = runner._open_book[position.trade_id]  # type: ignore[attr-defined]
    runner.trade_manager.apply_exit_evaluation(
        position.trade_id,  # type: ignore[attr-defined]
        ExitEvaluation(
            kind=ExitKind.STOP,
            reason_code=ReasonCode.OK,
            detail="SIMULATED audit close",
            updated_policy=position.exit_policy,  # type: ignore[attr-defined]
        ),
    )
    pending = runner.trade_manager.get_position(position.trade_id)  # type: ignore[attr-defined]
    assert pending is not None
    exits = runner._submit_exit(intent, decision, pending, snapshots)
    return exits, intent


def _reconstruct_m3_gross(store: TradingStore, session_date: date) -> Decimal:
    book = FourModeBook.reconstruct_from_store(store, session_date)
    ledger = book.get_ledger(ModeId.M3_TACTICAL_POSITIONAL)
    assert ledger.realized_pnl_today == ledger.realized_gross_pnl_today
    return ledger.realized_gross_pnl_today.amount


def _order_cash_flow(events: tuple[object, ...]) -> Decimal:
    total = Decimal(0)
    for event in events:
        fill = event.average_fill_price  # type: ignore[attr-defined]
        if fill is None:
            continue
        value = fill.value * Decimal(event.filled_quantity)  # type: ignore[attr-defined]
        side = event.command.side  # type: ignore[attr-defined]
        total += value if side is Side.SELL else -value
    return total


class TestP0ClosedMultilegPreservesEntryFillsAndPnl:
    """m3_m4_20260925 P0: close must not drop entry legs or corrupt restart P&L."""

    def test_bull_put_close_keeps_entry_legs_and_fill_ledger_pnl(
        self, tmp_path: Path
    ) -> None:
        from tests.structures import open_bull_put_credit
        from trading.storage.trading_store import TradingEventType
        from trading.domain.contracts.order import OrderEvent

        clock = FrozenClock(NOW + timedelta(seconds=60))
        store = TradingStore.open(tmp_path / "closed-pnl.sqlite", clock=clock)
        try:
            runner, position, snapshots = open_bull_put_credit(store, clock)
            entry_events = tuple(
                stored.deserialize()
                for stored in store.read_events()
                if stored.event_type is TradingEventType.ORDER_EVENT
            )
            entry_events = tuple(
                event
                for event in entry_events
                if isinstance(event, OrderEvent)
                and event.identity.trade_id == position.trade_id
                and event.state.name == "FILLED"
            )
            exits, intent = _close_open_position(runner, position, snapshots)
            closed = runner.trade_manager.get_position(position.trade_id)
            lifecycle = store.get_position_lifecycle(position.trade_id)
            assert closed is not None and closed.state is TradeState.CLOSED
            assert lifecycle is not None
            assert len(intent.legs) == 2
            assert len(lifecycle.position.legs) == 2
            assert lifecycle.position.entry_legs is not None
            assert len(lifecycle.position.entry_legs) == 2

            actual_gross = _order_cash_flow((*entry_events, *exits))
            assert actual_gross == Decimal("-22.50")
            assert _reconstruct_m3_gross(store, clock.now_utc().date()) == actual_gross
        finally:
            store.close()

    def test_fresh_runner_reconstruction_matches_fill_ledger_gross(
        self, tmp_path: Path
    ) -> None:
        from tests.structures import open_bull_put_credit
        from tests.test_four_mode_session_integration import _runner

        clock = FrozenClock(NOW + timedelta(seconds=60))
        store = TradingStore.open(tmp_path / "restart-pnl.sqlite", clock=clock)
        try:
            runner, position, snapshots = open_bull_put_credit(store, clock)
            _close_open_position(runner, position, snapshots)
            replacement = _runner(store, clock)
            replacement.recover_lifecycle()
            assert _reconstruct_m3_gross(store, clock.now_utc().date()) == Decimal(
                "-22.50"
            )
        finally:
            store.close()

    @pytest.mark.parametrize(
        ("opener", "mode_id", "leg_count"),
        [
            ("open_bull_put_credit", ModeId.M3_TACTICAL_POSITIONAL, 2),
            ("open_bull_call_debit", ModeId.M3_TACTICAL_POSITIONAL, 2),
            ("open_iron_condor", ModeId.M4_STRATEGIC_POSITIONAL, 4),
            ("open_call_butterfly", ModeId.M4_STRATEGIC_POSITIONAL, 3),
        ],
    )
    def test_structure_close_retains_immutable_entry_leg_history(
        self,
        tmp_path: Path,
        opener: str,
        mode_id: ModeId,
        leg_count: int,
    ) -> None:
        import tests.structures as structures

        clock = FrozenClock(NOW + timedelta(seconds=60))
        store = TradingStore.open(tmp_path / f"{opener}.sqlite", clock=clock)
        try:
            open_fn = getattr(structures, opener)
            runner, position, snapshots = open_fn(store, clock)
            _close_open_position(runner, position, snapshots)
            lifecycle = store.get_position_lifecycle(position.trade_id)
            assert lifecycle is not None
            assert len(lifecycle.position.legs) == leg_count
            assert lifecycle.position.entry_legs is not None
            assert len(lifecycle.position.entry_legs) == leg_count
            for frozen, current in zip(
                lifecycle.position.entry_legs,
                lifecycle.position.legs,
                strict=True,
            ):
                assert frozen.leg_id == current.leg_id
                assert frozen.average_entry_price == current.average_entry_price
                assert frozen.quantity_contracts == current.quantity_contracts
            book = FourModeBook.reconstruct_from_store(store, clock.now_utc().date())
            ledger = book.get_ledger(mode_id)
            assert ledger.realized_gross_pnl_today == ledger.realized_pnl_today
        finally:
            store.close()

    def test_exit_pending_restart_does_not_double_count_partial_fills(
        self, tmp_path: Path
    ) -> None:
        from tests.structures import open_bull_put_credit
        from tests.test_four_mode_session_integration import _runner
        from trading.domain.contracts.order_plan import OrderPlan

        clock = FrozenClock(NOW + timedelta(seconds=60))
        store = TradingStore.open(tmp_path / "partial-exit.sqlite", clock=clock)
        try:
            runner, position, snapshots = open_bull_put_credit(store, clock)
            intent, decision = runner._open_book[position.trade_id]
            runner.trade_manager.apply_exit_evaluation(
                position.trade_id,
                ExitEvaluation(
                    kind=ExitKind.STOP,
                    reason_code=ReasonCode.OK,
                    detail="partial close",
                    updated_policy=position.exit_policy,
                ),
            )
            pending = runner.trade_manager.get_position(position.trade_id)
            assert pending is not None
            plan = runner._exit_plan(intent, decision, pending, snapshots)
            assert plan is not None and plan.orders
            first_plan = OrderPlan.model_validate(
                {**plan.model_dump(), "orders": (plan.orders[0],)}
            )
            submit = runner._services.oms.submit_plan(
                first_plan,
                strategy_id=intent.strategy_id,
                account_id=runner._account.config.account_id,
            )
            for event in submit.events:
                runner._services.trade_manager.apply_exit_order_event(
                    event,
                    capital_reservation_id=decision.capital_reservation_id,
                    remaining_stays_open=True,
                )
            # Partial exits that return to OPEN must not leave stale exit ids.
            runner._write_lifecycle(position.trade_id, exit_order_ids=())
            mid = runner.trade_manager.get_position(position.trade_id)
            assert mid is not None and mid.state is TradeState.OPEN
            assert _reconstruct_m3_gross(store, clock.now_utc().date()) == Decimal(0)

            clock.set(clock.now_utc() + timedelta(seconds=30))
            replacement = _runner(store, clock)
            replacement.recover_lifecycle()
            assert _reconstruct_m3_gross(store, clock.now_utc().date()) == Decimal(0)

            resumed = replacement.trade_manager.get_position(position.trade_id)
            assert resumed is not None
            for snapshot in snapshots.values():
                replacement._services.broker.publish_quote(
                    snapshot.contract.symbol, snapshot.market  # type: ignore[attr-defined]
                )
            _close_open_position(replacement, resumed, snapshots)
            assert _reconstruct_m3_gross(store, clock.now_utc().date()) == Decimal(
                "-22.50"
            )
        finally:
            store.close()

    def test_replayed_order_events_stay_idempotent_for_realized_gross(
        self, tmp_path: Path
    ) -> None:
        from tests.structures import open_bull_put_credit
        from trading.domain.primitives import Currency
        from trading.risk.mode_ledger import (
            _index_order_events,
            _order_dedupe_key,
            _trade_fill_cash_flow,
        )

        clock = FrozenClock(NOW + timedelta(seconds=60))
        store = TradingStore.open(tmp_path / "replay.sqlite", clock=clock)
        try:
            runner, position, snapshots = open_bull_put_credit(store, clock)
            _close_open_position(runner, position, snapshots)
            before = _reconstruct_m3_gross(store, clock.now_utc().date())
            assert before == Decimal("-22.50")
            indexed = _index_order_events(store)
            replayed = dict(indexed)
            for event in indexed.values():
                if event.identity.trade_id != position.trade_id:
                    continue
                replayed[_order_dedupe_key(event)] = event
            gross = _trade_fill_cash_flow(
                position.trade_id, replayed, Currency.INR
            ).amount
            assert gross == before
        finally:
            store.close()

    def test_daily_loss_budget_uses_reconstructed_net_after_restart(
        self, tmp_path: Path
    ) -> None:
        from tests.structures import open_bull_put_credit
        from tests.test_four_mode_session_integration import _runner
        from trading.domain.contracts.mode_policy import load_modes_config

        clock = FrozenClock(NOW + timedelta(seconds=60))
        store = TradingStore.open(tmp_path / "daily-loss.sqlite", clock=clock)
        try:
            runner, position, snapshots = open_bull_put_credit(store, clock)
            _close_open_position(runner, position, snapshots)
            _runner(store, clock).recover_lifecycle()
            modes = load_modes_config(ROOT / "config" / "modes.yaml")
            fraction = modes.modes[
                ModeId.M3_TACTICAL_POSITIONAL
            ].daily_budget_cap_fraction
            book = FourModeBook.reconstruct_from_store(store, clock.now_utc().date())
            ledger = book.get_ledger(ModeId.M3_TACTICAL_POSITIONAL)
            assert ledger.realized_gross_pnl_today.amount == Decimal("-22.50")
            assert ledger.realized_pnl_today.amount == Decimal("-22.50")
            budget = ledger.daily_loss_budget(fraction)
            zero = Money.zero(Currency.INR)
            expected = max(budget + ledger.realized_pnl_today, zero)
            assert ledger.daily_loss_remaining(fraction) == expected
            assert not ledger.daily_loss_breached(fraction)
        finally:
            store.close()


class TestP0EntryHoldKeepsExitsAvailable:
    """Audit hold: new PAPER entries suspended, exits/recovery preserved."""

    def test_deployed_session_config_holds_new_entries(self) -> None:
        cfg = load_paper_session_config(ROOT / "config" / "paper_session.yaml")
        assert cfg.new_entries_enabled is False

    def test_hold_keeps_the_exit_path_live_in_the_same_tick(
        self, tmp_path: Path
    ) -> None:
        """Entry hold suspends new exposure only; exits must still fire."""
        clock = FrozenClock(NOW + timedelta(seconds=60))
        store = TradingStore.open(tmp_path / "entry-hold.sqlite", clock=clock)
        try:
            runner = _open_reviewable_long(store, clock)
            opened = runner.trade_manager.list_positions()[0]
            contract = opened.legs[0].contract
            # A stop-through quote: the continuous exit must close the trade
            # even though new entries are held.
            stopped = _review_snapshot(contract, at=SLOT_1030).model_copy(
                update={"market": f.quote(bid=f.price("1.00"), ask=f.price("1.05"))}
            )
            sink = _Sink()
            session = _review_session(
                runner, clock, tmp_path, sink, {contract.symbol: stopped}
            )
            assert session.session_config.new_entries_enabled is False
            clock.set(SLOT_1030)
            session.tick()
            closed = runner.trade_manager.get_position(opened.trade_id)
            assert closed is not None
            assert closed.state is TradeState.CLOSED
        finally:
            store.close()

    def test_hold_blocks_new_entry_submission(self, tmp_path: Path) -> None:
        """A held session must not submit an entry order for a fresh request."""
        from tests.test_paper_runner import _request as _entry_request

        clock = FrozenClock(NOW + timedelta(seconds=60))
        store = TradingStore.open(tmp_path / "entry-hold-block.sqlite", clock=clock)
        try:
            from tests.test_paper_lifecycle import _runner as _lifecycle_runner

            runner = _lifecycle_runner(store, clock)
            request = _entry_request()
            snapshots = {
                request.candidates[0].contract.symbol: request.candidates[0],
                request.underlying.contract.symbol: request.underlying,
            }
            session_cfg = load_paper_session_config(
                ROOT / "config" / "paper_session.yaml"
            )

            def builder(
                _now: datetime,
            ) -> tuple[tuple[object, ...], dict[str, FeatureSnapshot]]:
                return ((request,), snapshots)

            session = PaperSession(
                runner=runner,
                clock=clock,
                session_config=session_cfg,
                session_hours=(time(9, 15), time(15, 30)),
                timezone=IST,
                notifier=_Sink(),
                request_builder=builder,  # type: ignore[arg-type]
                observation_start=NOW,
                capital_limit=Money.of("700000", Currency.INR),
                risk_policy_version="6",
                fill_model_version="conservative-v1",
                code_version="audit-remediation",
                charges_verified=False,
                cohort_dir=tmp_path / "cohorts",
            )
            session.tick()
            assert runner.trade_manager.list_positions() == ()
            assert runner.broker.list_orders() == ()
        finally:
            store.close()
