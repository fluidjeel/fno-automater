"""ADESK-D3: PORTFOLIO BOUNDED for config-promotion only (C1)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from trading.ai.portfolio_desk import (
    PORTFOLIO_CONFIG_POLICY_VERSION,
    PORTFOLIO_CONFIG_PROMPT_VERSION,
    PORTFOLIO_MODEL_ID,
    maybe_log_portfolio_config_promotion,
    portfolio_bounded_actions_allowed,
)
from trading.domain.contracts.authority import AuthorityGrant
from trading.domain.enums import (
    BOUNDED_ACTIONS,
    CONFIG_PROMOTION_ACTIONS,
    LIVE_PATH_ACTIONS,
    AgentAction,
    AuthorityMode,
    DeskRole,
    Environment,
)

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


def _bounded_grant(**overrides: object) -> AuthorityGrant:
    payload: dict[str, object] = {
        "grant_id": "GRANT-PF-BOUNDED",
        "role": DeskRole.PORTFOLIO,
        "mode": AuthorityMode.BOUNDED,
        "allowed_actions": (
            AgentAction.PROPOSE_FAMILY_ENABLE,
            AgentAction.PROPOSE_FAMILY_HALT,
        ),
        "strategy_families": ("positional_long_option",),
        "environment": Environment.PAPER,
        "policy_version": PORTFOLIO_CONFIG_POLICY_VERSION,
        "prompt_version": PORTFOLIO_CONFIG_PROMPT_VERSION,
        "model_id": PORTFOLIO_MODEL_ID,
        "evidence_report_id": "EV-D3",
        "granted_at": NOW - timedelta(hours=1),
        "valid_until": NOW + timedelta(days=30),
        "signed_by": "operator@desk",
    }
    payload.update(overrides)
    return AuthorityGrant.issue(**payload)


def test_bounded_actions_alias_is_config_promotion() -> None:
    assert BOUNDED_ACTIONS == CONFIG_PROMOTION_ACTIONS
    assert BOUNDED_ACTIONS.isdisjoint(LIVE_PATH_ACTIONS)


def test_portfolio_bounded_actions_helper() -> None:
    assert portfolio_bounded_actions_allowed(
        (AgentAction.PROPOSE_FAMILY_HALT,)
    )
    assert not portfolio_bounded_actions_allowed((AgentAction.VETO_ENTRY,))
    assert not portfolio_bounded_actions_allowed(())


def test_bounded_grant_rejects_veto_as_order() -> None:
    with pytest.raises(ValidationError, match="live-path"):
        _bounded_grant(allowed_actions=(AgentAction.VETO_ENTRY,))


def test_bounded_grant_rejects_approve_style_live_path() -> None:
    """Approve-as-order / submit cannot appear on a BOUNDED portfolio grant."""
    with pytest.raises(ValidationError, match="live-path"):
        _bounded_grant(allowed_actions=(AgentAction.SUBMIT_ORDER,))
    with pytest.raises(ValidationError, match="live-path"):
        _bounded_grant(allowed_actions=(AgentAction.REDUCE_SIZE,))


def test_config_promotion_logs_at_bounded() -> None:
    grant = _bounded_grant()
    result = maybe_log_portfolio_config_promotion(
        as_of=NOW,
        action=AgentAction.PROPOSE_FAMILY_HALT,
        strategy_family="positional_long_option",
        decision_log=None,
        enabled=True,
        run_id="r1",
        snapshot_id="snap-1",
        grant=grant,
        now=NOW,
    )
    assert result.status == "LOGGED"
    assert result.mode is AuthorityMode.BOUNDED
    assert result.decision is not None
    assert result.decision.action is AgentAction.PROPOSE_FAMILY_HALT
    assert result.decision.mode is AuthorityMode.BOUNDED
    assert result.decision.gate_outcome.value == "ACCEPTED"


def test_live_path_action_rejected_by_helper() -> None:
    grant = _bounded_grant()
    result = maybe_log_portfolio_config_promotion(
        as_of=NOW,
        action=AgentAction.VETO_ENTRY,
        strategy_family="positional_long_option",
        decision_log=None,
        enabled=True,
        run_id="r2",
        snapshot_id="snap-2",
        grant=grant,
        now=NOW,
    )
    assert result.status == "REJECTED_NOT_BOUNDED"
    assert result.decision is None


def test_without_grant_observes() -> None:
    result = maybe_log_portfolio_config_promotion(
        as_of=NOW,
        action=AgentAction.PROPOSE_FAMILY_ENABLE,
        strategy_family="positional_long_option",
        decision_log=None,
        enabled=True,
        run_id="r3",
        snapshot_id="snap-3",
        grant=None,
        now=NOW,
    )
    assert result.status == "OBSERVE"
    assert result.mode is AuthorityMode.OBSERVE
