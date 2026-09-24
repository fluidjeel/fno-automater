"""ADESK-D4: POSITION SHADOW/ADVISORY only for tighten/partial — refuse BOUNDED."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from trading.ai.packets import DeltaPacket, build_delta_packet
from trading.ai.position import (
    POSITION_LIVE_PATH_ACTIONS,
    POSITION_MODEL_ID,
    POSITION_POLICY_VERSION,
    POSITION_PROMPT_VERSION,
    maybe_log_position_shadow,
    refuse_bounded_position_live_path,
)
from trading.domain.contracts.authority import AuthorityGrant
from trading.domain.enums import (
    AgentAction,
    AuthorityMode,
    DeskRole,
    Environment,
    ReviewAction,
    ReviewSlotId,
)

NOW = datetime(2026, 9, 20, 10, 30, tzinfo=UTC)


def _packet() -> DeltaPacket:
    return build_delta_packet(
        role=DeskRole.POSITION,
        as_of=NOW,
        snapshot_id="snap-pos",
        trade_id="T-1",
        spot=Decimal("24500"),
        unrealized_r=Decimal("0.4"),
        question="Tighten or hold?",
    )


def _grant(*, mode: AuthorityMode, actions: tuple[AgentAction, ...]) -> AuthorityGrant:
    return AuthorityGrant.issue(
        grant_id="GRANT-POS",
        role=DeskRole.POSITION,
        mode=mode,
        allowed_actions=actions,
        strategy_families=("positional_long_option",),
        environment=Environment.PAPER,
        policy_version=POSITION_POLICY_VERSION,
        prompt_version=POSITION_PROMPT_VERSION,
        model_id=POSITION_MODEL_ID,
        evidence_report_id="EV-D4",
        granted_at=NOW - timedelta(hours=1),
        valid_until=NOW + timedelta(days=14),
        signed_by="operator@desk",
    )


def test_bounded_tighten_stop_grant_rejected_at_validate() -> None:
    with pytest.raises(ValidationError, match="live-path"):
        _grant(mode=AuthorityMode.BOUNDED, actions=(AgentAction.TIGHTEN_STOP,))


def test_bounded_partial_exit_grant_rejected_at_validate() -> None:
    with pytest.raises(ValidationError, match="live-path"):
        _grant(mode=AuthorityMode.BOUNDED, actions=(AgentAction.PARTIAL_EXIT,))


def test_shadow_grant_allows_tighten() -> None:
    grant = _grant(mode=AuthorityMode.SHADOW, actions=(AgentAction.TIGHTEN_STOP,))
    result = maybe_log_position_shadow(
        _packet(),
        slot_id=ReviewSlotId.NSE_MORNING,
        deterministic_action=ReviewAction.TIGHTEN_STOP,
        decision_log=None,
        enabled=True,
        run_id="r1",
        grant=grant,
        now=NOW,
    )
    assert result.status == "LOGGED"
    assert result.mode is AuthorityMode.SHADOW
    assert result.decision is not None
    assert result.decision.action is AgentAction.TIGHTEN_STOP


def test_advisory_grant_allows_partial() -> None:
    grant = _grant(mode=AuthorityMode.ADVISORY, actions=(AgentAction.PARTIAL_EXIT,))
    result = maybe_log_position_shadow(
        _packet(),
        slot_id=ReviewSlotId.NSE_AFTERNOON,
        deterministic_action=ReviewAction.PARTIAL_EXIT,
        decision_log=None,
        enabled=True,
        run_id="r2",
        grant=grant,
        now=NOW,
    )
    assert result.status == "LOGGED"
    assert result.mode is AuthorityMode.ADVISORY
    assert result.decision is not None
    assert result.decision.action is AgentAction.PARTIAL_EXIT


def test_refuse_bounded_helper() -> None:
    assert refuse_bounded_position_live_path(
        AuthorityMode.BOUNDED, AgentAction.TIGHTEN_STOP
    )
    assert refuse_bounded_position_live_path(
        AuthorityMode.BOUNDED, AgentAction.PARTIAL_EXIT
    )
    assert not refuse_bounded_position_live_path(
        AuthorityMode.ADVISORY, AgentAction.TIGHTEN_STOP
    )
    assert {
        AgentAction.TIGHTEN_STOP,
        AgentAction.PARTIAL_EXIT,
    } <= POSITION_LIVE_PATH_ACTIONS
