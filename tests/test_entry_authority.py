"""ADESK-D5: ENTRY SHADOW/ADVISORY for veto/reduce; BOUNDED only config-promotion."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from trading.ai.entry import (
    ENTRY_CONFIG_POLICY_VERSION,
    ENTRY_CONFIG_PROMPT_VERSION,
    ENTRY_LIVE_PATH_ACTIONS,
    ENTRY_MODEL_ID,
    ENTRY_POLICY_VERSION,
    ENTRY_PROMPT_VERSION,
    maybe_log_entry_config_promotion,
    refuse_bounded_entry_live_path,
)
from trading.domain.contracts.authority import AuthorityGrant
from trading.domain.enums import (
    AgentAction,
    AuthorityMode,
    DeskRole,
    Environment,
)

NOW = datetime(2026, 9, 20, 10, 0, tzinfo=UTC)


def _grant(*, mode: AuthorityMode, actions: tuple[AgentAction, ...]) -> AuthorityGrant:
    prompt = (
        ENTRY_CONFIG_PROMPT_VERSION
        if mode is AuthorityMode.BOUNDED
        else ENTRY_PROMPT_VERSION
    )
    policy = (
        ENTRY_CONFIG_POLICY_VERSION
        if mode is AuthorityMode.BOUNDED
        else ENTRY_POLICY_VERSION
    )
    return AuthorityGrant.issue(
        grant_id="GRANT-ENTRY",
        role=DeskRole.ENTRY,
        mode=mode,
        allowed_actions=actions,
        strategy_families=("positional_long_option",),
        environment=Environment.PAPER,
        policy_version=policy,
        prompt_version=prompt,
        model_id=ENTRY_MODEL_ID,
        evidence_report_id="EV-D5",
        granted_at=NOW - timedelta(hours=1),
        valid_until=NOW + timedelta(days=14),
        signed_by="operator@desk",
    )


def test_bounded_veto_entry_rejected_at_validate() -> None:
    with pytest.raises(ValidationError, match="live-path"):
        _grant(mode=AuthorityMode.BOUNDED, actions=(AgentAction.VETO_ENTRY,))


def test_bounded_reduce_size_rejected_at_validate() -> None:
    with pytest.raises(ValidationError, match="live-path"):
        _grant(mode=AuthorityMode.BOUNDED, actions=(AgentAction.REDUCE_SIZE,))


def test_shadow_and_advisory_may_list_veto() -> None:
    shadow = _grant(mode=AuthorityMode.SHADOW, actions=(AgentAction.VETO_ENTRY,))
    advisory = _grant(
        mode=AuthorityMode.ADVISORY, actions=(AgentAction.VETO_ENTRY,)
    )
    assert shadow.mode is AuthorityMode.SHADOW
    assert advisory.mode is AuthorityMode.ADVISORY


def test_refuse_bounded_helper() -> None:
    assert refuse_bounded_entry_live_path(
        AuthorityMode.BOUNDED, AgentAction.VETO_ENTRY
    )
    assert refuse_bounded_entry_live_path(
        AuthorityMode.BOUNDED, AgentAction.REDUCE_SIZE
    )
    assert not refuse_bounded_entry_live_path(
        AuthorityMode.ADVISORY, AgentAction.VETO_ENTRY
    )
    assert {
        AgentAction.VETO_ENTRY,
        AgentAction.REDUCE_SIZE,
    } == ENTRY_LIVE_PATH_ACTIONS


def test_config_promotion_bounded_ok() -> None:
    grant = _grant(
        mode=AuthorityMode.BOUNDED,
        actions=(AgentAction.PROPOSE_FAMILY_ENABLE,),
    )
    result = maybe_log_entry_config_promotion(
        as_of=NOW,
        action=AgentAction.PROPOSE_FAMILY_ENABLE,
        strategy_family="positional_long_option",
        decision_log=None,
        enabled=True,
        run_id="r1",
        snapshot_id="snap-1",
        grant=grant,
    )
    assert result.status == "LOGGED"
    assert result.mode is AuthorityMode.BOUNDED
    assert result.decision is not None
    assert result.decision.action is AgentAction.PROPOSE_FAMILY_ENABLE


def test_veto_rejected_on_config_promotion_path() -> None:
    grant = _grant(
        mode=AuthorityMode.BOUNDED,
        actions=(AgentAction.PROPOSE_FAMILY_HALT,),
    )
    result = maybe_log_entry_config_promotion(
        as_of=NOW,
        action=AgentAction.VETO_ENTRY,
        strategy_family="positional_long_option",
        decision_log=None,
        enabled=True,
        run_id="r2",
        snapshot_id="snap-2",
        grant=grant,
    )
    assert result.status == "REJECTED_NOT_BOUNDED"
    assert result.decision is None
