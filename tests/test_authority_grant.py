"""ADESK-A1: AuthorityGrant, C1 BOUNDED partition, and demotion to OBSERVE.

Invariant 2: a grant cannot place an order or bypass a gate.
Invariant 7: missing/expired/mismatched AI authority is OBSERVE, never an error.
Decision C1 (2026-09-20): BOUNDED is config-promotion only; live-path actions
are rejected at grant write/validate time.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from trading.ai.authority import resolve_effective_mode
from trading.domain.clock import FrozenClock
from trading.domain.contracts.authority import (
    MAX_AUTHORITY_GRANT_LIFETIME,
    AuthorityGrant,
)
from trading.domain.enums import (
    ADVISORY_ACTIONS,
    CONFIG_PROMOTION_ACTIONS,
    LIVE_PATH_ACTIONS,
    AgentAction,
    AuthorityMode,
    DeskRole,
    Environment,
    FamilyStance,
)
from trading.storage.trading_store import TradingStore

NOW = datetime(2026, 9, 20, 6, 0, tzinfo=UTC)
MODEL = "deepseek-chat-20250301"
PROMPT = "desk-prompt-v1"
POLICY = "desk-policy-v1"


def _grant_kwargs(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "grant_id": "GRANT-PORTFOLIO-1",
        "role": DeskRole.PORTFOLIO,
        "mode": AuthorityMode.BOUNDED,
        "allowed_actions": (AgentAction.PROPOSE_FAMILY_HALT,),
        "strategy_families": ("positional_long_option",),
        "environment": Environment.PAPER,
        "policy_version": POLICY,
        "prompt_version": PROMPT,
        "model_id": MODEL,
        "evidence_report_id": "SCORECARD-1",
        "granted_at": NOW,
        "valid_until": NOW + timedelta(days=30),
        "signed_by": "operator@desk",
    }
    payload.update(overrides)
    return payload


def _issue(**overrides: object) -> AuthorityGrant:
    return AuthorityGrant.issue(**_grant_kwargs(**overrides))


@pytest.fixture
def store(tmp_path: Path) -> Iterator[TradingStore]:
    trading_store = TradingStore.open(
        tmp_path / "authority.sqlite", clock=FrozenClock(NOW)
    )
    yield trading_store
    trading_store.close()


class TestAgentActionPartition:
    def test_partitions_are_complete_and_disjoint(self) -> None:
        """Every AgentAction is config-promotion, live-path, or advisory."""
        members = frozenset(AgentAction)
        partitioned = CONFIG_PROMOTION_ACTIONS | LIVE_PATH_ACTIONS | ADVISORY_ACTIONS
        assert partitioned == members
        assert CONFIG_PROMOTION_ACTIONS.isdisjoint(LIVE_PATH_ACTIONS)
        assert CONFIG_PROMOTION_ACTIONS.isdisjoint(ADVISORY_ACTIONS)
        assert LIVE_PATH_ACTIONS.isdisjoint(ADVISORY_ACTIONS)

    def test_config_promotion_maps_onto_family_stance(self) -> None:
        """C1 BOUNDED vocabulary is the existing STRATEGY_FAMILY stances."""
        assert (
            AgentAction.PROPOSE_FAMILY_ENABLE.to_family_stance() is FamilyStance.ENABLE
        )
        assert (
            AgentAction.PROPOSE_FAMILY_SHADOW.to_family_stance() is FamilyStance.SHADOW
        )
        assert AgentAction.PROPOSE_FAMILY_HALT.to_family_stance() is FamilyStance.HALT
        for action in CONFIG_PROMOTION_ACTIONS:
            assert action.is_config_promotion
            assert not action.is_live_path

    def test_live_path_includes_order_influence(self) -> None:
        """Size, stop, submit, veto, tighten, exit, roll, hedge, add are live-path."""
        live = {
            AgentAction.VETO_ENTRY,
            AgentAction.REDUCE_SIZE,
            AgentAction.TIGHTEN_STOP,
            AgentAction.PARTIAL_EXIT,
            AgentAction.FULL_EXIT,
            AgentAction.SUBMIT_ORDER,
            AgentAction.PROPOSE_ROLL,
            AgentAction.PROPOSE_HEDGE,
            AgentAction.PROPOSE_SIZE_INCREASE,
            AgentAction.PROPOSE_ADD,
        }
        assert live == LIVE_PATH_ACTIONS
        for action in live:
            assert action.is_live_path
            assert not action.is_config_promotion


class TestBoundedGrantValidation:
    def test_bounded_tighten_stop_is_rejected(self) -> None:
        """C1: BOUNDED + TIGHTEN_STOP is live-path and must not validate."""
        with pytest.raises(ValidationError, match="live-path"):
            _issue(allowed_actions=(AgentAction.TIGHTEN_STOP,))

    def test_bounded_veto_entry_is_rejected(self) -> None:
        """C1: veto-as-order is live-path; SAFETY_INVARIANTS override the desk spec."""
        with pytest.raises(ValidationError, match="live-path"):
            _issue(allowed_actions=(AgentAction.VETO_ENTRY,))

    def test_bounded_submit_and_size_are_rejected(self) -> None:
        """C1: submit and size actions cannot appear on a BOUNDED grant."""
        with pytest.raises(ValidationError, match="live-path"):
            _issue(allowed_actions=(AgentAction.SUBMIT_ORDER,))
        with pytest.raises(ValidationError, match="live-path"):
            _issue(allowed_actions=(AgentAction.REDUCE_SIZE, AgentAction.PROPOSE_ADD))

    def test_bounded_config_promotion_only_is_accepted(self) -> None:
        """BOUNDED with enable/shadow/halt FamilyStance actions is valid on PAPER."""
        grant = _issue(
            allowed_actions=(
                AgentAction.PROPOSE_FAMILY_ENABLE,
                AgentAction.PROPOSE_FAMILY_SHADOW,
                AgentAction.PROPOSE_FAMILY_HALT,
            ),
            strategy_families=("positional_long_option", "cas_microstructure"),
        )
        assert grant.mode is AuthorityMode.BOUNDED
        assert all(action.is_config_promotion for action in grant.allowed_actions)
        assert len(grant.checksum) == 64

    def test_bounded_live_environment_is_rejected(self) -> None:
        """C1 Stage A: LIVE + BOUNDED needs a dedicated signed record."""
        with pytest.raises(ValidationError, match=r"Environment\.LIVE"):
            _issue(environment=Environment.LIVE)

    def test_lifetime_over_90_days_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="90 days"):
            _issue(
                valid_until=NOW + MAX_AUTHORITY_GRANT_LIFETIME + timedelta(seconds=1)
            )

    def test_valid_until_must_follow_granted_at(self) -> None:
        with pytest.raises(ValidationError, match="after granted_at"):
            _issue(valid_until=NOW)

    def test_wrong_checksum_is_rejected(self) -> None:
        signed = _issue()
        with pytest.raises(ValidationError, match="checksum"):
            AuthorityGrant.model_validate(
                {**signed.model_dump(mode="json"), "checksum": "0" * 64}
            )

    def test_shadow_may_list_live_path_actions(self) -> None:
        """SHADOW never reaches the gate; C1 live-path reject is BOUNDED-only."""
        grant = _issue(
            mode=AuthorityMode.SHADOW,
            allowed_actions=(AgentAction.TIGHTEN_STOP,),
        )
        assert grant.mode is AuthorityMode.SHADOW


class TestResolveEffectiveMode:
    def test_no_grant_is_observe(self) -> None:
        """Invariant 7: missing grant is OBSERVE, never an error."""
        mode = resolve_effective_mode(
            DeskRole.PORTFOLIO,
            runtime_model_id=MODEL,
            runtime_prompt_version=PROMPT,
            runtime_policy_version=POLICY,
            now=NOW,
            grant=None,
        )
        assert mode is AuthorityMode.OBSERVE

    def test_expired_grant_is_observe(self) -> None:
        """Expiry silently demotes; there is no auto-renewal."""
        grant = _issue(valid_until=NOW + timedelta(days=1))
        mode = resolve_effective_mode(
            DeskRole.PORTFOLIO,
            runtime_model_id=MODEL,
            runtime_prompt_version=PROMPT,
            runtime_policy_version=POLICY,
            now=NOW + timedelta(days=1),
            grant=grant,
        )
        assert mode is AuthorityMode.OBSERVE

    def test_triple_mismatch_is_observe(self) -> None:
        """A new model, prompt or policy is a new agent and must re-earn authority."""
        grant = _issue()
        model_mismatch = resolve_effective_mode(
            DeskRole.PORTFOLIO,
            runtime_model_id="other-model",
            runtime_prompt_version=PROMPT,
            runtime_policy_version=POLICY,
            now=NOW,
            grant=grant,
        )
        prompt_mismatch = resolve_effective_mode(
            DeskRole.PORTFOLIO,
            runtime_model_id=MODEL,
            runtime_prompt_version="prompt-v2",
            runtime_policy_version=POLICY,
            now=NOW,
            grant=grant,
        )
        policy_mismatch = resolve_effective_mode(
            DeskRole.PORTFOLIO,
            runtime_model_id=MODEL,
            runtime_prompt_version=PROMPT,
            runtime_policy_version="policy-v2",
            now=NOW,
            grant=grant,
        )
        assert model_mismatch is AuthorityMode.OBSERVE
        assert prompt_mismatch is AuthorityMode.OBSERVE
        assert policy_mismatch is AuthorityMode.OBSERVE

    def test_matching_unexpired_bounded_returns_bounded(self) -> None:
        grant = _issue()
        mode = resolve_effective_mode(
            DeskRole.PORTFOLIO,
            runtime_model_id=MODEL,
            runtime_prompt_version=PROMPT,
            runtime_policy_version=POLICY,
            now=NOW,
            grant=grant,
        )
        assert mode is AuthorityMode.BOUNDED

    def test_role_mismatch_is_observe_not_an_error(self) -> None:
        grant = _issue(role=DeskRole.PORTFOLIO)
        mode = resolve_effective_mode(
            DeskRole.ENTRY,
            runtime_model_id=MODEL,
            runtime_prompt_version=PROMPT,
            runtime_policy_version=POLICY,
            now=NOW,
            grant=grant,
        )
        assert mode is AuthorityMode.OBSERVE


class TestAuthorityGrantStore:
    def test_round_trip(self, store: TradingStore) -> None:
        grant = _issue()
        store.insert_authority_grant(grant)
        loaded = store.get_authority_grant(grant.grant_id)
        assert loaded == grant
        listed = store.list_authority_grants(role=DeskRole.PORTFOLIO)
        assert listed == (grant,)
        assert store.list_authority_grants(role=DeskRole.ENTRY) == ()

    def test_write_rejects_bounded_live_path(self, store: TradingStore) -> None:
        """C1 is enforced at write even if construction validators were bypassed."""
        illegal = AuthorityGrant.model_construct(
            **_grant_kwargs(  # type: ignore[arg-type]
                allowed_actions=(AgentAction.TIGHTEN_STOP,),
                checksum="0" * 64,
            )
        )
        with pytest.raises(ValidationError, match="live-path"):
            store.insert_authority_grant(illegal)
        assert store.get_authority_grant("GRANT-PORTFOLIO-1") is None
