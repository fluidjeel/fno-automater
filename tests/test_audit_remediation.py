"""Positive product regressions for four_mode_20260925 + m3_m4_20260925.

Each test asserts required production behaviour on the real session → Layer 2
→ PAPER OMS → exit/restart path. Tests exercise production classes; they do not
reimplement domain logic or expect pre-remediation defects.

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
from trading.domain.enums import (
    FamilyId,
    ModeId,
    OptionType,
    ReasonCode,
    ReviewAction,
    Side,
    TradeState,
)
from trading.trade.roll_switch import begin_roll_switch_transition
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
    """four_mode_20260925 §8.8 / m3_m4_20260925 P1: deterministic exits survive AI timeout."""

    def test_session_tick_survives_shadow_timeout_and_keeps_protection(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Invariant 8: a shadow timeout does not abort the production tick.

        Runs the real session path (PaperSession.tick -> _run_due_reviews ->
        PaperRunner.run_review_slot) and expects protection and review evidence
        to persist when shadow logging raises TimeoutError.
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
        """A review FULL_EXIT under AI timeout still closes and submits SELL legs."""
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
    """m3_m4_20260925 P0: exit plans cover shorts before releasing protection."""

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
            assert ledger.realized_pnl_today.amount <= ledger.realized_gross_pnl_today.amount
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
        from trading.portfolio.fill_ledger import trade_fill_cash_flow
        from trading.risk.mode_ledger import (
            _index_order_events,
            _order_dedupe_key,
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
            gross = trade_fill_cash_flow(
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
            assert ledger.realized_pnl_today.amount < ledger.realized_gross_pnl_today.amount
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


class TestP0OpenRiskCapsAndAtomicReservations:
    """four_mode_20260925 P0: global/mode caps, try_reserve, restart open risk."""

    def test_session_config_keeps_new_entries_frozen(self) -> None:
        cfg = load_paper_session_config(ROOT / "config" / "paper_session.yaml")
        assert cfg.new_entries_enabled is False

    def test_global_and_mode_caps_are_enforced_in_layer2(self) -> None:
        from trading.config.risk_policy import load_risk_policy
        from trading.domain.contracts.mode_policy import load_modes_config
        from trading.domain.primitives import Rounding
        from trading.risk.mode_ledger import FourModeBook

        limits_source = (ROOT / "src/trading/risk/limits.py").read_text(encoding="utf-8")
        gateway_source = (ROOT / "src/trading/risk/gateway.py").read_text(
            encoding="utf-8"
        )
        assert "max_open_loss_cap_fraction" in limits_source
        assert "max_global_open_risk" in limits_source
        assert "try_reserve" in gateway_source
        policy = load_risk_policy(ROOT / "config" / "risk.yaml").config
        modes = load_modes_config(ROOT / "config" / "modes.yaml")
        assert policy.max_global_open_risk is not None
        assert policy.max_global_open_risk.to_money().amount == Decimal("35000")
        book = FourModeBook(modes_config=modes)
        global_cap = policy.max_global_open_risk.to_money()
        # Mode open-risk property is what Layer 2 compares against the mode cap.
        m2 = book.get_ledger(ModeId.M2_DIRECTIONAL)
        mode_cap = (
            m2.reference_capital
            * modes.modes[ModeId.M2_DIRECTIONAL].max_open_loss_cap_fraction
        ).quantized(Rounding.FLOOR)
        assert book.try_reserve(ModeId.M2_DIRECTIONAL, mode_cap)
        assert book.get_ledger(ModeId.M2_DIRECTIONAL).open_risk == mode_cap
        # Global envelope rejects a second hold that would breach ₹35,000.
        remainder = global_cap - mode_cap
        assert remainder.amount > 0
        assert not book.try_reserve(
            ModeId.M3_TACTICAL_POSITIONAL,
            remainder + Money.of("1", Currency.INR),
            global_cap=global_cap,
        )
        assert book.try_reserve(
            ModeId.M3_TACTICAL_POSITIONAL,
            remainder,
            global_cap=global_cap,
        )
        assert book.total_open_risk() == global_cap

    def test_try_reserve_failure_releases_durable_hold_and_blocks_submit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tests.test_four_mode_session_integration import _runner
        from tests.test_four_mode_trade_simulations import _macro, _option, _request
        from trading.domain.enums import FamilyId, OptionType, RiskAction
        from trading.strategies.macro import MacroBias

        clock = FrozenClock(NOW + timedelta(seconds=60))
        store = TradingStore.open(tmp_path / "try-reserve.sqlite", clock=clock)
        try:
            runner = _runner(store, clock)
            book = runner._services.gateway.mode_book
            assert book is not None
            monkeypatch.setattr(book, "try_reserve", lambda *_a, **_k: False)
            option = _option("24000", OptionType.CALL)
            result = runner.run_cycle(
                (
                    _request(
                        strategy_id="positional_long_option",
                        candidates=(option,),
                        mode_id=ModeId.M2_DIRECTIONAL,
                        family_id=FamilyId.long_call,
                        macro=_macro(MacroBias.BULLISH),
                    ),
                )
            )
            outcome = result.outcomes[0]
            assert outcome.decisions
            decision = outcome.decisions[0]
            assert decision.action is RiskAction.REJECT
            assert ReasonCode.CAPITAL_UNAVAILABLE in decision.reason_codes
            assert outcome.order_events == ()
            holding = [
                res
                for res in store.list_reservations()
                if res.intent_id == decision.intent_id and res.state.holds_capital
            ]
            assert not holding
        finally:
            store.close()

    def test_simultaneous_mode_proposals_respect_global_cap(
        self, tmp_path: Path
    ) -> None:
        from concurrent.futures import ThreadPoolExecutor
        from trading.domain.contracts.mode_policy import load_modes_config
        from trading.risk.mode_ledger import FourModeBook

        modes = load_modes_config(ROOT / "config" / "modes.yaml")
        book = FourModeBook(modes_config=modes)
        global_cap = Money.of("35000", Currency.INR)
        chunk = Money.of("20000", Currency.INR)
        modes_order = (
            ModeId.M1_CAS,
            ModeId.M2_DIRECTIONAL,
            ModeId.M3_TACTICAL_POSITIONAL,
            ModeId.M4_STRATEGIC_POSITIONAL,
        )

        def _attempt(mode_id: ModeId) -> bool:
            return book.try_reserve(mode_id, chunk, global_cap=global_cap)

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(_attempt, modes_order))
        assert sum(1 for ok in results if ok) == 1
        assert book.total_open_risk() == chunk
        # A fifth attempt after the winners still fails closed.
        assert not book.try_reserve(ModeId.M2_DIRECTIONAL, chunk, global_cap=global_cap)

    def test_fill_moves_reservation_to_margin_exactly_once(
        self, tmp_path: Path
    ) -> None:
        from tests.structures import open_bull_put_credit

        clock = FrozenClock(NOW + timedelta(seconds=60))
        store = TradingStore.open(tmp_path / "fill-once.sqlite", clock=clock)
        try:
            runner, position, _snapshots = open_bull_put_credit(store, clock)
            book = runner._services.gateway.mode_book
            assert book is not None
            ledger = book.get_ledger(ModeId.M3_TACTICAL_POSITIONAL)
            intent, decision = runner._open_book[position.trade_id]
            assert decision.recalculated_max_loss is not None
            assert ledger.reserved_capital.amount == Decimal(0)
            assert ledger.margin_used == decision.recalculated_max_loss
            # Duplicate fill notification must not inflate margin.
            before = ledger.margin_used
            runner._services.gateway.note_mode_fill(
                ModeId.M3_TACTICAL_POSITIONAL,
                margin=decision.recalculated_max_loss,
                trade_id=position.trade_id,
            )
            after = book.get_ledger(ModeId.M3_TACTICAL_POSITIONAL).margin_used
            assert after == before
            assert book.total_open_risk() == before
        finally:
            store.close()

    def test_close_releases_open_risk(self, tmp_path: Path) -> None:
        from tests.structures import open_bull_put_credit

        clock = FrozenClock(NOW + timedelta(seconds=60))
        store = TradingStore.open(tmp_path / "close-release.sqlite", clock=clock)
        try:
            runner, position, snapshots = open_bull_put_credit(store, clock)
            book = runner._services.gateway.mode_book
            assert book is not None
            assert book.total_open_risk().amount > 0
            _close_open_position(runner, position, snapshots)
            assert book.total_open_risk().amount == Decimal(0)
        finally:
            store.close()

    def test_failed_protected_prefix_releases_reservation(
        self, tmp_path: Path
    ) -> None:
        from tests.test_four_mode_session_integration import _runner
        from tests.test_four_mode_trade_simulations import _spec
        from tests.test_p10_iron_condor_binder_and_g2 import _macro as m4_macro
        from tests.test_p11_m4_broad_basket import _call_butterfly_candidates
        from trading.domain.enums import ExecutionMode, FamilyId, OptionType
        from trading.runtime.paper_runner import PaperStrategyRequest

        clock = FrozenClock(NOW + timedelta(seconds=60))
        store = TradingStore.open(tmp_path / "abort-prefix.sqlite", clock=clock)
        try:
            candidates = tuple(
                item.model_copy(
                    update={"market": item.market.model_copy(update={"last": None})}
                )
                for item in _call_butterfly_candidates()
            )
            runner = _runner(store, clock)
            before = runner._services.gateway.mode_book.total_open_risk()  # type: ignore[union-attr]
            runner.run_cycle(
                (
                    PaperStrategyRequest(
                        strategy_id=FamilyId.long_call_butterfly.value,
                        underlying=f.snapshot(
                            snapshot_id="SNAP-UNDER",
                            contract=f.index_contract(),
                            market=f.quote(last=f.price("24500"), close=f.price("24500")),
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
                        experiment_id="EXP-ABORT",
                        execution_mode=ExecutionMode.PAPER,
                        macro=m4_macro(),
                        execute=True,
                        forced_family_id=FamilyId.long_call_butterfly,
                    ),
                )
            )
            after = runner._services.gateway.mode_book.total_open_risk()  # type: ignore[union-attr]
            assert after == before
            assert runner.trade_manager.list_positions() == ()
        finally:
            store.close()

    def test_restart_reconstructs_open_risk_without_double_counting(
        self, tmp_path: Path
    ) -> None:
        from tests.structures import open_bull_put_credit
        from tests.test_four_mode_session_integration import _runner

        clock = FrozenClock(NOW + timedelta(seconds=60))
        store = TradingStore.open(tmp_path / "restart-risk.sqlite", clock=clock)
        try:
            runner, position, _snapshots = open_bull_put_credit(store, clock)
            _, decision = runner._open_book[position.trade_id]
            live = runner._services.gateway.mode_book
            assert live is not None
            live_risk = live.total_open_risk()
            live_ledger = live.get_ledger(ModeId.M3_TACTICAL_POSITIONAL)
            assert live_ledger.reserved_capital.amount == Decimal(0)
            assert live_ledger.margin_used == decision.recalculated_max_loss

            restarted = _runner(store, clock)
            restarted.recover_lifecycle()
            book = restarted._services.gateway.mode_book
            assert book is not None
            ledger = book.get_ledger(ModeId.M3_TACTICAL_POSITIONAL)
            assert ledger.margin_used == decision.recalculated_max_loss
            assert ledger.reserved_capital.amount == Decimal(0)
            assert book.total_open_risk() == live_risk
            # No double count: open_risk == margin when reserved is zero.
            assert book.total_open_risk() == ledger.margin_used
        finally:
            store.close()

    def test_restart_mid_entry_restores_reserved_not_margin(
        self, tmp_path: Path
    ) -> None:
        from trading.domain.contracts.mode_policy import load_modes_config
        from trading.domain.enums import ReservationState
        from trading.domain.contracts.reservation import CapitalReservation
        from trading.risk.mode_ledger import FourModeBook

        clock = FrozenClock(NOW + timedelta(seconds=60))
        store = TradingStore.open(tmp_path / "mid-entry.sqlite", clock=clock)
        try:
            hold = CapitalReservation(
                reservation_id="RES-MID",
                intent_id="INT-MID",
                strategy_id="positional_long_option",
                mode_id=ModeId.M2_DIRECTIONAL,
                idempotency_key="INT-MID",
                state=ReservationState.RESERVED,
                amount=Money.of("5000", Currency.INR),
                created_at=clock.now_utc(),
                updated_at=clock.now_utc(),
            )
            store.upsert_reservation(hold)
            # A committed twin must not inflate reserved after restart.
            committed = hold.model_copy(
                update={
                    "reservation_id": "RES-COMMITTED",
                    "intent_id": "INT-OPEN",
                    "idempotency_key": "INT-OPEN",
                    "state": ReservationState.COMMITTED,
                    "amount": Money.of("8000", Currency.INR),
                }
            )
            store.upsert_reservation(committed)
            book = FourModeBook.reconstruct_from_store(
                store, clock.now_utc().date(), modes_config=load_modes_config(ROOT / "config" / "modes.yaml")
            )
            m2 = book.get_ledger(ModeId.M2_DIRECTIONAL)
            assert m2.reserved_capital.amount == Decimal("5000")
            assert m2.margin_used.amount == Decimal(0)
            assert book.total_open_risk().amount == Decimal("5000")
        finally:
            store.close()


class TestP0M3M4FollowingWeekCandidates:
    """m3_m4_20260925: M3/M4 must not discard ≥7-DTE following-week rows."""

    def test_following_week_rows_reach_m3_and_m4(self) -> None:
        from trading.runtime.four_mode_producers import _candidates_for_mode

        base = f.snapshot(
            snapshot_id="SNAP-FW",
            contract=f.option_contract(symbol="NIFTY26OCT24000CE", strike=Decimal("24000")),
            derivatives=DerivativesContext(
                days_to_expiry=9,
                open_interest=5000,
                option_type=OptionType.CALL,
                underlying_price=f.price("24000"),
            ),
        )
        marked = base.model_copy(
            update={"features": {**base.features, "following_week_chain": Decimal(1)}}
        )
        assert _candidates_for_mode((marked,), mode_id=ModeId.M2_DIRECTIONAL) == (
            marked,
        )
        assert _candidates_for_mode((marked,), mode_id=ModeId.M3_TACTICAL_POSITIONAL) == (
            marked,
        )
        assert _candidates_for_mode(
            (marked,), mode_id=ModeId.M4_STRATEGIC_POSITIONAL
        ) == (marked,)
        assert _candidates_for_mode((marked,), mode_id=ModeId.M1_CAS) == ()


class TestP0M1EventPathAndM2Carry:
    """four_mode_20260925: M1 event path + M2 carry gate on the session builder."""

    def test_stale_m1_provider_quote_is_rejected(self, tmp_path: Path) -> None:
        from dataclasses import replace

        from tests.test_m1_paper_session_integration import (
            _load_validated_session,
            _m1_chain,
            _m1_fixture_builder,
            _session,
            _simulated_event,
        )
        from tests.test_p7_m1_selector_and_g3_path import _times
        from zoneinfo import ZoneInfo

        at = datetime(2026, 9, 14, 15, 15, tzinfo=IST).astimezone(ZoneInfo("UTC"))
        clock = FrozenClock(at)
        store = TradingStore.open(tmp_path / "m1-stale.sqlite", clock=clock)
        try:
            underlying, call, instruments = _m1_chain()
            underlying = underlying.model_copy(update={"times": _times(at)})
            call = call.model_copy(update={"times": _times(at)})
            session_cfg = _load_validated_session()
            builder = _m1_fixture_builder(
                repo_root=tmp_path,
                session_cfg=session_cfg,
                option_candidates=(call,),
                underlying=underlying,
                instruments=instruments,
            )
            session = _session(
                store,
                clock,
                tmp_path,
                repo_root=tmp_path,
                session_cfg=session_cfg,
                builder=builder,
            )
            event = replace(_simulated_event(now=at), quote_time=at - timedelta(seconds=10))
            result = session.submit_m1_event(event)
            assert result is None or not any(
                outcome.order_events for outcome in (result.outcomes if result else ())
            )
            assert session._runner.trade_manager.list_positions() == ()
        finally:
            store.close()

    def test_m2_carry_gate_approves_on_session_path(self, tmp_path: Path) -> None:
        from tests.test_four_mode_session_integration import _runner
        from tests.test_four_mode_trade_simulations import _macro, _option, _request
        from tests.test_p7_m1_selector_and_g3_path import _times
        from trading.domain.enums import CarryGateAction, FamilyId
        from trading.strategies.macro import MacroBias

        entry_at = NOW + timedelta(seconds=60)
        clock = FrozenClock(entry_at)
        store = TradingStore.open(tmp_path / "m2-carry.sqlite", clock=clock)
        try:
            runner = _runner(store, clock)
            option = _option("24000", OptionType.CALL)
            opened = runner.run_cycle(
                (
                    _request(
                        strategy_id="positional_long_option",
                        candidates=(option,),
                        mode_id=ModeId.M2_DIRECTIONAL,
                        family_id=FamilyId.long_call,
                        macro=_macro(MacroBias.BULLISH),
                    ),
                )
            )
            assert opened.outcomes[0].order_events
            carry_at = datetime(2026, 9, 14, 15, 1, tzinfo=IST)
            clock.set(carry_at.astimezone(UTC))
            fresh = option.model_copy(update={"times": _times(carry_at.astimezone(UTC))})
            session_cfg = load_paper_session_config(ROOT / "config" / "paper_session.yaml")

            def builder(
                _now: datetime,
            ) -> tuple[tuple[object, ...], dict[str, FeatureSnapshot]]:
                return ((), {fresh.contract.symbol: fresh})

            session = PaperSession(
                runner=runner,
                clock=clock,
                session_config=session_cfg,
                session_hours=(time(9, 15), time(15, 30)),
                timezone=IST,
                notifier=_Sink(),
                request_builder=builder,  # type: ignore[arg-type]
                observation_start=entry_at,
                capital_limit=Money.of("2500000", Currency.INR),
                risk_policy_version="6",
                fill_model_version="conservative-v1",
                code_version="audit-remediation",
                charges_verified=False,
                cohort_dir=tmp_path / "cohorts",
            )
            session._run_due_m2_carry_gate({fresh.contract.symbol: fresh}, carry_at.astimezone(UTC))
            records = store.list_position_lifecycle()
            carry = records[-1].carry_records[-1]
            assert carry.action is CarryGateAction.CARRY_APPROVED
        finally:
            store.close()


class TestP0RollSwitchPerLegAndReplacement:
    """M4 ROLL/SWITCH: per-leg close quantities and L2-gated replacement."""

    @staticmethod
    def _roll_evaluation():
        from trading.trade.review import ReviewEvaluation

        return ReviewEvaluation(
            action=ReviewAction.PROPOSE_ROLL,
            reason_code=ReasonCode.OK,
            detail="audit roll close",
            submit_structure_close=True,
            roll_switch_kind=ReviewAction.ROLL,
        )

    def _close_via_roll(
        self,
        runner: PaperRunner,
        position: object,
        snapshots: dict[str, FeatureSnapshot],
    ) -> None:
        intent, decision = runner._open_book[position.trade_id]  # type: ignore[attr-defined]
        submitted = runner._apply_review(
            self._roll_evaluation(),
            intent=intent,
            decision=decision,
            position=position,  # type: ignore[arg-type]
            snapshots=snapshots,
            review_id="REV-ROLL",
        )
        assert submitted is True

    @pytest.mark.parametrize(
        "opener",
        ["open_bull_put_credit", "open_bull_call_debit", "open_iron_condor", "open_call_butterfly"],
    )
    def test_roll_close_plan_uses_per_leg_quantities(
        self, tmp_path: Path, opener: str
    ) -> None:
        import tests.structures as structures

        clock = FrozenClock(NOW + timedelta(seconds=60))
        store = TradingStore.open(tmp_path / f"roll-{opener}.sqlite", clock=clock)
        try:
            runner, position, snapshots = getattr(structures, opener)(store, clock)
            intent, decision = runner._open_book[position.trade_id]
            plan = runner._exit_plan(intent, decision, position, snapshots)
            assert plan is not None
            qty_by_leg = {
                leg.leg_id: leg.quantity_contracts for leg in position.legs
            }
            for order in plan.orders:
                assert order.command.quantity_contracts == qty_by_leg[order.leg_id]
        finally:
            store.close()

    def test_close_only_review_stays_propose_roll_not_roll(self, tmp_path: Path) -> None:
        from tests.structures import open_bull_call_debit
        from trading.domain.enums import RollSwitchStatus

        clock = FrozenClock(NOW + timedelta(seconds=60))
        store = TradingStore.open(tmp_path / "roll-label.sqlite", clock=clock)
        try:
            runner, position, snapshots = open_bull_call_debit(store, clock)
            self._close_via_roll(runner, position, snapshots)
            lifecycle = store.get_position_lifecycle(position.trade_id)
            assert lifecycle is not None
            assert lifecycle.roll_switch_transition is not None
            assert lifecycle.roll_switch_transition.kind is ReviewAction.ROLL
            assert lifecycle.roll_switch_transition.status in {
                RollSwitchStatus.CLOSE_COMPLETE,
                RollSwitchStatus.REPLACEMENT_PENDING_L2,
            }
            closed = runner.trade_manager.get_position(position.trade_id)
            assert closed is not None and closed.state is TradeState.CLOSED
        finally:
            store.close()

    def test_replacement_rejection_does_not_complete_roll(self, tmp_path: Path) -> None:
        from dataclasses import replace

        from tests.structures import open_bull_call_debit
        from tests.test_four_mode_trade_simulations import _macro, _request
        from trading.domain.enums import RollSwitchStatus
        from trading.strategies.macro import MacroBias

        clock = FrozenClock(NOW + timedelta(seconds=60))
        store = TradingStore.open(tmp_path / "roll-reject.sqlite", clock=clock)
        try:
            runner, position, snapshots = open_bull_call_debit(store, clock)
            self._close_via_roll(runner, position, snapshots)
            legs = tuple(snapshots.values())
            blocked = replace(
                _request(
                    strategy_id="debit_spread",
                    candidates=legs,
                    mode_id=ModeId.M3_TACTICAL_POSITIONAL,
                    family_id=FamilyId.bull_call_debit,
                    macro=_macro(MacroBias.BULLISH),
                ),
                execute=False,
            )
            outcome = runner.submit_roll_switch_replacement(position.trade_id, blocked)
            assert outcome.approved is False
            lifecycle = store.get_position_lifecycle(position.trade_id)
            assert lifecycle is not None
            assert lifecycle.roll_switch_transition is not None
            assert (
                lifecycle.roll_switch_transition.status
                is RollSwitchStatus.REPLACEMENT_REJECTED
            )
        finally:
            store.close()

    def test_replacement_l2_approval_completes_roll(self, tmp_path: Path) -> None:
        from tests.structures import open_bull_call_debit
        from tests.test_four_mode_trade_simulations import _macro, _request
        from trading.domain.enums import RollSwitchStatus
        from trading.strategies.macro import MacroBias

        clock = FrozenClock(NOW + timedelta(seconds=60))
        store = TradingStore.open(tmp_path / "roll-approve.sqlite", clock=clock)
        try:
            runner, position, snapshots = open_bull_call_debit(store, clock)
            self._close_via_roll(runner, position, snapshots)
            legs = tuple(snapshots.values())
            replacement = _request(
                strategy_id="debit_spread",
                candidates=legs,
                mode_id=ModeId.M3_TACTICAL_POSITIONAL,
                family_id=FamilyId.bull_call_debit,
                macro=_macro(MacroBias.BULLISH),
            )
            outcome = runner.submit_roll_switch_replacement(
                position.trade_id, replacement
            )
            assert outcome.approved is True
            assert outcome.replacement_trade_id is not None
            lifecycle = store.get_position_lifecycle(position.trade_id)
            assert lifecycle is not None
            assert lifecycle.roll_switch_transition is not None
            assert lifecycle.roll_switch_transition.status is RollSwitchStatus.COMPLETE
        finally:
            store.close()

    def test_unknown_exit_blocks_replacement(self, tmp_path: Path) -> None:
        from tests.structures import open_bull_call_debit
        from tests.test_four_mode_trade_simulations import _macro, _request
        from trading.domain.enums import RollSwitchStatus
        from trading.strategies.macro import MacroBias

        clock = FrozenClock(NOW + timedelta(seconds=60))
        store = TradingStore.open(tmp_path / "roll-unknown.sqlite", clock=clock)
        try:
            runner, position, snapshots = open_bull_call_debit(store, clock)
            self._close_via_roll(runner, position, snapshots)
            runner._write_lifecycle(
                position.trade_id,
                roll_switch_transition=begin_roll_switch_transition(
                    kind=ReviewAction.ROLL,
                    review_id="REV-UNK",
                    as_of=clock.now_utc(),
                ).model_copy(update={"status": RollSwitchStatus.CLOSE_PENDING}),
            )
            runner._ensure_entries_blocked(
                ReasonCode.UNKNOWN_ORDER_STATUS,
                "exit order status is unknown; replacement is blocked until reconciliation",
            )
            legs = tuple(snapshots.values())
            replacement = _request(
                strategy_id="debit_spread",
                candidates=legs,
                mode_id=ModeId.M3_TACTICAL_POSITIONAL,
                family_id=FamilyId.bull_call_debit,
                macro=_macro(MacroBias.BULLISH),
            )
            outcome = runner.submit_roll_switch_replacement(
                position.trade_id, replacement
            )
            assert outcome.approved is False
            assert ReasonCode.UNKNOWN_ORDER_STATUS in outcome.reason_codes
        finally:
            store.close()

    def test_restart_during_exit_pending_resumes_close(self, tmp_path: Path) -> None:
        from tests.structures import open_bull_put_credit
        from tests.test_four_mode_session_integration import _runner
        from trading.domain.contracts.order_plan import OrderPlan
        from trading.domain.enums import RollSwitchStatus

        clock = FrozenClock(NOW + timedelta(seconds=60))
        store = TradingStore.open(tmp_path / "roll-restart.sqlite", clock=clock)
        try:
            runner, position, snapshots = open_bull_put_credit(store, clock)
            intent, decision = runner._open_book[position.trade_id]
            runner.trade_manager.apply_exit_evaluation(
                position.trade_id,
                ExitEvaluation(
                    kind=ExitKind.STOP,
                    reason_code=ReasonCode.OK,
                    detail="partial roll close",
                    updated_policy=position.exit_policy,
                ),
            )
            pending = runner.trade_manager.get_position(position.trade_id)
            assert pending is not None
            plan = runner._exit_plan(intent, decision, pending, snapshots)
            assert plan is not None and len(plan.orders) >= 2
            first_plan = OrderPlan.model_validate(
                {**plan.model_dump(), "orders": (plan.orders[0],)}
            )
            runner._write_lifecycle(
                position.trade_id,
                roll_switch_transition=begin_roll_switch_transition(
                    kind=ReviewAction.ROLL,
                    review_id="REV-RESTART",
                    as_of=clock.now_utc(),
                ),
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
            runner._write_lifecycle(position.trade_id, exit_order_ids=())
            mid = runner.trade_manager.get_position(position.trade_id)
            assert mid is not None and mid.state is TradeState.OPEN

            clock.set(clock.now_utc() + timedelta(seconds=30))
            resumed = _runner(store, clock)
            resumed.recover_lifecycle()
            for snapshot in snapshots.values():
                resumed._services.broker.publish_quote(
                    snapshot.contract.symbol, snapshot.market
                )
            book = resumed._open_book[position.trade_id]
            intent, decision = book
            pending = resumed.trade_manager.get_position(position.trade_id)
            assert pending is not None
            resumed.trade_manager.apply_exit_evaluation(
                position.trade_id,
                ExitEvaluation(
                    kind=ExitKind.STOP,
                    reason_code=ReasonCode.OK,
                    detail="resume remaining roll close",
                    updated_policy=pending.exit_policy,
                ),
            )
            pending = resumed.trade_manager.get_position(position.trade_id)
            assert pending is not None
            resumed._submit_exit(intent, decision, pending, snapshots)
            closed = resumed.trade_manager.get_position(position.trade_id)
            assert closed is not None
            assert closed.state is TradeState.CLOSED
            lifecycle = store.get_position_lifecycle(position.trade_id)
            assert lifecycle is not None
            assert lifecycle.roll_switch_transition is not None
            assert lifecycle.roll_switch_transition.status in {
                RollSwitchStatus.CLOSE_COMPLETE,
                RollSwitchStatus.REPLACEMENT_PENDING_L2,
            }
        finally:
            store.close()


class TestP0CampaignDrawdownRollChain:
    """Campaign P&L and loss limits survive roll/switch trade-id changes."""

    @staticmethod
    def _roll_evaluation():
        from trading.trade.review import ReviewEvaluation

        return ReviewEvaluation(
            action=ReviewAction.PROPOSE_ROLL,
            reason_code=ReasonCode.OK,
            detail="campaign roll close",
            submit_structure_close=True,
            roll_switch_kind=ReviewAction.ROLL,
        )

    def _close_via_roll(
        self,
        runner: PaperRunner,
        position: object,
        snapshots: dict[str, FeatureSnapshot],
    ) -> None:
        intent, decision = runner._open_book[position.trade_id]  # type: ignore[attr-defined]
        assert runner._apply_review(
            self._roll_evaluation(),
            intent=intent,
            decision=decision,
            position=position,  # type: ignore[arg-type]
            snapshots=snapshots,
            review_id="REV-CAMP",
        )

    def test_campaign_id_shared_across_roll_replacement(self, tmp_path: Path) -> None:
        from tests.structures import open_bull_call_debit
        from tests.test_four_mode_trade_simulations import _macro, _request
        from trading.strategies.macro import MacroBias

        clock = FrozenClock(NOW + timedelta(seconds=60))
        store = TradingStore.open(tmp_path / "camp-share.sqlite", clock=clock)
        try:
            runner, position, snapshots = open_bull_call_debit(store, clock)
            incumbent = store.get_position_lifecycle(position.trade_id)
            assert incumbent is not None and incumbent.campaign_id is not None
            self._close_via_roll(runner, position, snapshots)
            replacement = _request(
                strategy_id="debit_spread",
                candidates=tuple(snapshots.values()),
                mode_id=ModeId.M3_TACTICAL_POSITIONAL,
                family_id=FamilyId.bull_call_debit,
                macro=_macro(MacroBias.BULLISH),
            )
            outcome = runner.submit_roll_switch_replacement(
                position.trade_id, replacement
            )
            assert outcome.approved is True
            assert outcome.replacement_trade_id is not None
            campaign = store.get_campaign_record(incumbent.campaign_id)
            assert campaign is not None
            assert position.trade_id in campaign.trade_ids
            assert outcome.replacement_trade_id in campaign.trade_ids
            replacement_lifecycle = store.get_position_lifecycle(
                outcome.replacement_trade_id
            )
            assert replacement_lifecycle is not None
            assert replacement_lifecycle.campaign_id == incumbent.campaign_id
            assert campaign.cumulative_realized_gross.currency == campaign.cumulative_realized_net.currency
            assert campaign.drawdown.amount >= 0
        finally:
            store.close()

    def test_multiple_rolls_keep_one_campaign(self, tmp_path: Path) -> None:
        from tests.structures import open_bull_call_debit
        from tests.test_four_mode_trade_simulations import _macro, _request
        from trading.strategies.macro import MacroBias

        clock = FrozenClock(NOW + timedelta(seconds=60))
        store = TradingStore.open(tmp_path / "camp-multi.sqlite", clock=clock)
        try:
            runner, position, snapshots = open_bull_call_debit(store, clock)
            lifecycle = store.get_position_lifecycle(position.trade_id)
            assert lifecycle is not None
            campaign_id = lifecycle.campaign_id
            assert campaign_id is not None
            trade_ids = [position.trade_id]
            for _ in range(2):
                current = runner.trade_manager.get_position(trade_ids[-1])
                assert current is not None
                self._close_via_roll(runner, current, snapshots)
                outcome = runner.submit_roll_switch_replacement(
                    trade_ids[-1],
                    _request(
                        strategy_id="debit_spread",
                        candidates=tuple(snapshots.values()),
                        mode_id=ModeId.M3_TACTICAL_POSITIONAL,
                        family_id=FamilyId.bull_call_debit,
                        macro=_macro(MacroBias.BULLISH),
                    ),
                )
                assert outcome.approved is True
                assert outcome.replacement_trade_id is not None
                trade_ids.append(outcome.replacement_trade_id)
                clock.set(clock.now_utc() + timedelta(seconds=30))
            campaign = store.get_campaign_record(campaign_id)
            assert campaign is not None
            assert len(campaign.trade_ids) == 3
            assert len(campaign.recorded_closes) == 2
        finally:
            store.close()

    def test_restart_preserves_campaign_between_close_and_replacement(
        self, tmp_path: Path
    ) -> None:
        from tests.structures import open_bull_call_debit
        from tests.test_four_mode_session_integration import _runner
        from tests.test_four_mode_trade_simulations import _macro, _request
        from trading.strategies.macro import MacroBias

        clock = FrozenClock(NOW + timedelta(seconds=60))
        store = TradingStore.open(tmp_path / "camp-restart.sqlite", clock=clock)
        try:
            runner, position, snapshots = open_bull_call_debit(store, clock)
            lifecycle = store.get_position_lifecycle(position.trade_id)
            assert lifecycle is not None
            campaign_id = lifecycle.campaign_id
            self._close_via_roll(runner, position, snapshots)
            before = store.get_campaign_record(campaign_id)
            assert before is not None
            clock.set(clock.now_utc() + timedelta(seconds=30))
            resumed = _runner(store, clock)
            resumed.recover_lifecycle()
            after = store.get_campaign_record(campaign_id)
            assert after is not None
            assert after.cumulative_realized_net == before.cumulative_realized_net
            outcome = resumed.submit_roll_switch_replacement(
                position.trade_id,
                _request(
                    strategy_id="debit_spread",
                    candidates=tuple(snapshots.values()),
                    mode_id=ModeId.M3_TACTICAL_POSITIONAL,
                    family_id=FamilyId.bull_call_debit,
                    macro=_macro(MacroBias.BULLISH),
                ),
            )
            assert outcome.approved is True
            assert outcome.replacement_trade_id is not None
            linked = store.get_campaign_record(campaign_id)
            assert linked is not None
            assert outcome.replacement_trade_id in linked.trade_ids
        finally:
            store.close()

    def test_restart_with_open_replacement_keeps_campaign(self, tmp_path: Path) -> None:
        from tests.structures import open_bull_call_debit
        from tests.test_four_mode_session_integration import _runner
        from tests.test_four_mode_trade_simulations import _macro, _request
        from trading.strategies.macro import MacroBias

        clock = FrozenClock(NOW + timedelta(seconds=60))
        store = TradingStore.open(tmp_path / "camp-open.sqlite", clock=clock)
        try:
            runner, position, snapshots = open_bull_call_debit(store, clock)
            lifecycle = store.get_position_lifecycle(position.trade_id)
            assert lifecycle is not None
            campaign_id = lifecycle.campaign_id
            self._close_via_roll(runner, position, snapshots)
            outcome = runner.submit_roll_switch_replacement(
                position.trade_id,
                _request(
                    strategy_id="debit_spread",
                    candidates=tuple(snapshots.values()),
                    mode_id=ModeId.M3_TACTICAL_POSITIONAL,
                    family_id=FamilyId.bull_call_debit,
                    macro=_macro(MacroBias.BULLISH),
                ),
            )
            assert outcome.approved is True
            replacement_id = outcome.replacement_trade_id
            assert replacement_id is not None
            before = store.get_campaign_record(campaign_id)
            clock.set(clock.now_utc() + timedelta(seconds=30))
            resumed = _runner(store, clock)
            resumed.recover_lifecycle()
            after = store.get_campaign_record(campaign_id)
            assert after is not None
            assert after.cumulative_realized_net == before.cumulative_realized_net
            assert replacement_id in after.trade_ids
            assert resumed.trade_manager.get_position(replacement_id) is not None
        finally:
            store.close()

    def test_rejected_replacement_preserves_campaign_losses(self, tmp_path: Path) -> None:
        from dataclasses import replace

        from tests.structures import open_bull_call_debit
        from tests.test_four_mode_trade_simulations import _macro, _request
        from trading.domain.enums import RollSwitchStatus
        from trading.strategies.macro import MacroBias

        clock = FrozenClock(NOW + timedelta(seconds=60))
        store = TradingStore.open(tmp_path / "camp-reject.sqlite", clock=clock)
        try:
            runner, position, snapshots = open_bull_call_debit(store, clock)
            lifecycle = store.get_position_lifecycle(position.trade_id)
            assert lifecycle is not None
            campaign_id = lifecycle.campaign_id
            self._close_via_roll(runner, position, snapshots)
            before = store.get_campaign_record(campaign_id)
            assert before is not None
            blocked = replace(
                _request(
                    strategy_id="debit_spread",
                    candidates=tuple(snapshots.values()),
                    mode_id=ModeId.M3_TACTICAL_POSITIONAL,
                    family_id=FamilyId.bull_call_debit,
                    macro=_macro(MacroBias.BULLISH),
                ),
                execute=False,
            )
            outcome = runner.submit_roll_switch_replacement(position.trade_id, blocked)
            assert outcome.approved is False
            after = store.get_campaign_record(campaign_id)
            assert after is not None
            assert after.cumulative_realized_net == before.cumulative_realized_net
            assert after.cumulative_realized_gross == before.cumulative_realized_gross
            assert after.entries_blocked is False
            closed_lifecycle = store.get_position_lifecycle(position.trade_id)
            assert closed_lifecycle is not None
            assert (
                closed_lifecycle.roll_switch_transition is not None
                and closed_lifecycle.roll_switch_transition.status
                is RollSwitchStatus.REPLACEMENT_REJECTED
            )
        finally:
            store.close()

    def test_duplicate_close_recording_is_idempotent(self, tmp_path: Path) -> None:
        from tests.structures import open_bull_call_debit

        clock = FrozenClock(NOW + timedelta(seconds=60))
        store = TradingStore.open(tmp_path / "camp-idem.sqlite", clock=clock)
        try:
            runner, position, snapshots = open_bull_call_debit(store, clock)
            lifecycle = store.get_position_lifecycle(position.trade_id)
            assert lifecycle is not None
            campaign_id = lifecycle.campaign_id
            self._close_via_roll(runner, position, snapshots)
            before = store.get_campaign_record(campaign_id)
            runner._record_campaign_close(position.trade_id)
            after = store.get_campaign_record(campaign_id)
            assert before is not None and after is not None
            assert after.cumulative_realized_net == before.cumulative_realized_net
            assert after.recorded_closes.count(position.trade_id) == 1
        finally:
            store.close()

    def test_campaign_loss_limit_blocks_replacement_after_deep_loss(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import tests.factories as f
        from tests.structures import open_bull_call_debit
        from tests.test_four_mode_trade_simulations import _macro, _request
        from trading.strategies.macro import MacroBias

        clock = FrozenClock(NOW + timedelta(seconds=60))
        store = TradingStore.open(tmp_path / "camp-limit.sqlite", clock=clock)
        try:
            monkeypatch.setattr(
                "trading.runtime.paper_runner.campaign_loss_limit",
                lambda mode_id, *, mode_book: f.money("100"),
            )
            runner, position, snapshots = open_bull_call_debit(store, clock)
            lifecycle = store.get_position_lifecycle(position.trade_id)
            assert lifecycle is not None
            campaign_id = lifecycle.campaign_id
            self._close_via_roll(runner, position, snapshots)
            campaign = store.get_campaign_record(campaign_id)
            assert campaign is not None
            assert campaign.entries_blocked is True
            outcome = runner.submit_roll_switch_replacement(
                position.trade_id,
                _request(
                    strategy_id="debit_spread",
                    candidates=tuple(snapshots.values()),
                    mode_id=ModeId.M3_TACTICAL_POSITIONAL,
                    family_id=FamilyId.bull_call_debit,
                    macro=_macro(MacroBias.BULLISH),
                ),
            )
            assert outcome.approved is False
            assert (
                ReasonCode.CAMPAIGN_LOSS_LIMIT in outcome.reason_codes
                or ReasonCode.UNKNOWN_ORDER_STATUS in outcome.reason_codes
            )
        finally:
            store.close()
