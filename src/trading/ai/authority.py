"""Deterministic authority demotion. Zero LLM calls; never blocks trading.

Invariant 7: missing, expired or mismatched AI authority falls back to the
deterministic baseline — here, AuthorityMode.OBSERVE. That is not an error.
"""

from __future__ import annotations

from datetime import datetime

from trading.domain.clock import ensure_utc
from trading.domain.contracts.authority import AuthorityGrant
from trading.domain.enums import AuthorityMode, DeskRole

__all__ = ["resolve_effective_mode"]


def resolve_effective_mode(
    role: DeskRole,
    *,
    runtime_model_id: str,
    runtime_prompt_version: str,
    runtime_policy_version: str,
    now: datetime,
    grant: AuthorityGrant | None,
) -> AuthorityMode:
    """Return the grant's mode, or OBSERVE when the grant cannot apply.

    Demotes when the grant is missing, expired, not yet valid, for a different
    role, or when (model_id, prompt_version, policy_version) disagree with the
    runtime triple. Never raises for those cases; never blocks trading.
    """
    instant = ensure_utc(now, "now")
    if grant is None:
        return AuthorityMode.OBSERVE
    if grant.role is not role:
        return AuthorityMode.OBSERVE
    if not (grant.granted_at <= instant < grant.valid_until):
        return AuthorityMode.OBSERVE
    runtime_triple = (
        runtime_model_id,
        runtime_prompt_version,
        runtime_policy_version,
    )
    grant_triple = (grant.model_id, grant.prompt_version, grant.policy_version)
    if runtime_triple != grant_triple:
        return AuthorityMode.OBSERVE
    return grant.mode
