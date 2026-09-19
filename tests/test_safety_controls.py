"""Safety controls and readiness (L2-009).

Invariant 6: stale snapshot blocks entry.
Invariant 9: RECOVERY blocks entries until reconciliation completes.
Invariant 24: kill-switch actions are deterministic and independently callable.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

import tests.factories as f
from trading.config import load_config
from trading.domain.clock import FrozenClock
from trading.domain.contracts import FeatureSnapshot
from trading.domain.enums import (
    DataQuality,
    ReadinessLevel,
    ReasonCode,
    SystemState,
    TradeState,
    Trigger,
)
from trading.domain.ids import SequentialIdFactory
from trading.safety import (
    ReadinessEvaluator,
    ReadinessRequest,
    SafetyControlKind,
    SafetyControls,
    daily_loss_cap_breached,
)

ROOT = Path(__file__).resolve().parent.parent
ACCOUNT_CONFIG = load_config(ROOT / "config" / "base.yaml")
NOW = f.NOW


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(NOW)


@pytest.fixture
def id_factory(clock: FrozenClock) -> SequentialIdFactory:
    return SequentialIdFactory(clock.instant)


@pytest.fixture
def controls(clock: FrozenClock, id_factory: SequentialIdFactory) -> SafetyControls:
    return SafetyControls(clock=clock, id_factory=id_factory)


def _ready_request(
    controls: SafetyControls,
    *,
    system_state: SystemState = SystemState.READY,
    entries_blocked: bool = False,
    feature: FeatureSnapshot | None = None,
    strategy_id: str | None = "positional_index_options_poc",
) -> ReadinessRequest:
    return ReadinessRequest(
        system_state=system_state,
        entries_blocked=entries_blocked,
        feature_snapshot=feature if feature is not None else f.snapshot(),
        safety_controls=controls,
        strategy_id=strategy_id,
        max_clock_drift_ms=ACCOUNT_CONFIG.config.freshness.max_clock_drift_ms,
    )


class TestDailyLossKillSwitch:
    def test_daily_loss_cap_breach_detected(self) -> None:
        """Invariant 24: realized loss at the cap trips the kill switch."""
        equity = f.money("700000")
        daily_cap = (equity.amount * Decimal("0.03")).quantize(Decimal("1"))
        portfolio = f.portfolio_snapshot(
            exposure=f.exposure(
                equity=equity,
                realized_pnl_today=f.money(str(-daily_cap)),
            )
        )
        assert daily_loss_cap_breached(portfolio, ACCOUNT_CONFIG.config.risk)

    def test_daily_loss_kill_switch_blocks_entries(
        self,
        controls: SafetyControls,
    ) -> None:
        """Invariant 24: kill switch is independently callable and blocks entries."""
        equity = f.money("700000")
        daily_cap = (equity.amount * Decimal("0.03")).quantize(Decimal("1"))
        portfolio = f.portfolio_snapshot(
            exposure=f.exposure(
                equity=equity,
                realized_pnl_today=f.money(str(-daily_cap)),
            )
        )
        event = controls.evaluate_daily_loss_kill_switch(
            portfolio,
            ACCOUNT_CONFIG.config.risk,
            scope="account/ACC-1",
        )
        assert event is not None
        assert event.kind is SafetyControlKind.DAILY_LOSS_KILL_SWITCH
        assert ReasonCode.KILL_SWITCH_ACTIVE in event.reason_codes
        assert controls.blocks_entry()
        assert controls.blocks_entry("positional_index_options_poc")

        report = ReadinessEvaluator().evaluate(_ready_request(controls))
        assert not report.entries_permitted
        assert ReasonCode.KILL_SWITCH_ACTIVE in report.reason_codes

    def test_kill_switch_latches_once(
        self,
        controls: SafetyControls,
    ) -> None:
        """Invariant 24: repeated evaluation does not emit duplicate events."""
        equity = f.money("700000")
        daily_cap = (equity.amount * Decimal("0.03")).quantize(Decimal("1"))
        portfolio = f.portfolio_snapshot(
            exposure=f.exposure(
                equity=equity,
                realized_pnl_today=f.money(str(-daily_cap)),
            )
        )
        first = controls.evaluate_daily_loss_kill_switch(
            portfolio,
            ACCOUNT_CONFIG.config.risk,
            scope="account/ACC-1",
        )
        second = controls.evaluate_daily_loss_kill_switch(
            portfolio,
            ACCOUNT_CONFIG.config.risk,
            scope="account/ACC-1",
        )
        assert first is not None
        assert second is None


class TestEntryFreezeAndStrategyHalt:
    def test_entry_freeze_blocks_entries_but_is_releasable(
        self,
        controls: SafetyControls,
    ) -> None:
        """Invariant 24: operator entry freeze is independently callable."""
        event = controls.freeze_entries(
            actor="operator",
            scope="account/ACC-1",
            incident_id="INC-1",
        )
        assert event.kind is SafetyControlKind.ENTRY_FREEZE
        assert controls.blocks_entry()
        assert controls.entry_block_reasons() == (ReasonCode.ENTRY_FROZEN,)

        controls.release_entry_freeze(
            actor="operator",
            scope="account/ACC-1",
            incident_id="INC-1",
        )
        assert not controls.blocks_entry()

    def test_strategy_halt_blocks_only_that_strategy(
        self,
        controls: SafetyControls,
    ) -> None:
        """Invariant 24: strategy halt scopes the block."""
        controls.halt_strategy(
            "positional_index_options_poc",
            actor="operator",
            scope="strategy/positional_index_options_poc",
        )
        assert controls.blocks_entry("positional_index_options_poc")
        assert not controls.blocks_entry("other_strategy")

    def test_global_halt_blocks_all_entries(
        self,
        controls: SafetyControls,
    ) -> None:
        """Invariant 24: global halt blocks every strategy."""
        controls.activate_global_halt(
            actor="operator",
            scope="account/ACC-1",
            trigger=Trigger.OPERATOR,
        )
        assert controls.blocks_entry()
        assert controls.blocks_entry("positional_index_options_poc")


class TestStaleSnapshotBlocksEntry:
    def test_stale_feature_snapshot_blocks_entry_ready(
        self,
        controls: SafetyControls,
    ) -> None:
        """Invariant 6: stale critical state blocks new exposure."""
        stale = f.snapshot(
            quality=f.quality(
                state=DataQuality.STALE,
                reason_codes=(ReasonCode.DATA_STALE,),
            )
        )
        assert not stale.permits_new_exposure

        report = ReadinessEvaluator().evaluate(_ready_request(controls, feature=stale))
        assert not report.entries_permitted
        assert ReasonCode.DATA_STALE in report.reason_codes
        assert not report.levels[ReadinessLevel.ENTRY_READY]

    def test_invalid_feature_snapshot_blocks_entry_ready(
        self,
        controls: SafetyControls,
    ) -> None:
        """Invariant 6: invalid critical state blocks new exposure."""
        invalid = f.snapshot(
            quality=f.quality(
                state=DataQuality.INVALID,
                reason_codes=(ReasonCode.DATA_INVALID,),
            )
        )
        report = ReadinessEvaluator().evaluate(
            _ready_request(controls, feature=invalid)
        )
        assert not report.entries_permitted
        assert ReasonCode.DATA_INVALID in report.reason_codes


class TestRecoveryGating:
    def test_recovery_blocks_entry_ready(
        self,
        controls: SafetyControls,
    ) -> None:
        """Invariant 9: RECOVERY blocks entries until reconciliation completes."""
        report = ReadinessEvaluator().evaluate(
            _ready_request(controls, system_state=SystemState.RECOVERY)
        )
        assert not report.entries_permitted
        assert ReasonCode.SYSTEM_NOT_READY in report.reason_codes
        assert not SystemState.RECOVERY.permits_new_exposure

    def test_ready_with_clean_checks_permits_entries(
        self,
        controls: SafetyControls,
    ) -> None:
        report = ReadinessEvaluator().evaluate(_ready_request(controls))
        assert report.entries_permitted
        assert report.levels[ReadinessLevel.ENTRY_READY]
        assert report.levels[ReadinessLevel.LIVE_SAFE]

    def test_reconciliation_block_persists_through_ready_state(
        self,
        controls: SafetyControls,
    ) -> None:
        """Invariant 9: unresolved reconciliation keeps entries blocked."""
        report = ReadinessEvaluator().evaluate(
            _ready_request(
                controls,
                system_state=SystemState.READY,
                entries_blocked=True,
            )
        )
        assert not report.entries_permitted
        assert ReasonCode.RECONCILIATION_UNRESOLVED in report.reason_codes
        assert not report.levels[ReadinessLevel.LIVE_SAFE]


class TestLiveSafe:
    def test_repair_required_position_blocks_live_safe(
        self,
        controls: SafetyControls,
    ) -> None:
        report = ReadinessEvaluator().evaluate(
            ReadinessRequest(
                system_state=SystemState.READY,
                entries_blocked=False,
                feature_snapshot=f.snapshot(),
                safety_controls=controls,
                open_positions=(f.position_state(state=TradeState.REPAIR_REQUIRED),),
            )
        )
        assert not report.levels[ReadinessLevel.LIVE_SAFE]
        assert not report.entries_permitted
