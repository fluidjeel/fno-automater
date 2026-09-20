"""Signed, expiring Agent Desk authority. Never a live-path instruction.

Invariant 2 and 22: a grant cannot place an order, mutate config, or bypass a
gate. Decision C1 (2026-09-20): AuthorityMode.BOUNDED is config-promotion only.
Intraday stays L3 + L2 with no LLM. BOUNDED may list only actions that propose
FamilyStance (enable/shadow/halt) for already-coded families via the L4
STRATEGY_FAMILY proposal path.

LIVE + BOUNDED is rejected here. A dedicated signed LIVE record is a later
operator artifact; Stage A does not model it. SAFETY_INVARIANTS override the
desk spec's older BOUNDED-eligible live-path list (veto/tighten/size).
"""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from typing import Any, Self

from pydantic import ValidationInfo, model_validator

from trading.domain.contracts.base import NonEmptyStr, UtcDatetime, VersionedModel
from trading.domain.enums import (
    AgentAction,
    AuthorityMode,
    DeskRole,
    Environment,
)

__all__ = [
    "MAX_AUTHORITY_GRANT_LIFETIME",
    "AuthorityGrant",
    "canonical_grant_checksum",
]

# Spec-mandated hard cap, not a market rule. There is no auto-renewal.
MAX_AUTHORITY_GRANT_LIFETIME = timedelta(days=90)


def canonical_grant_checksum(payload: dict[str, Any]) -> str:
    """SHA-256 of canonical JSON excluding the checksum field itself."""
    body = {key: value for key, value in payload.items() if key != "checksum"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class AuthorityGrant(VersionedModel):
    """Operator-signed desk authority. Agents cannot issue or renew this."""

    grant_id: NonEmptyStr
    role: DeskRole
    mode: AuthorityMode
    allowed_actions: tuple[AgentAction, ...]
    strategy_families: tuple[NonEmptyStr, ...]
    environment: Environment
    policy_version: NonEmptyStr
    prompt_version: NonEmptyStr
    model_id: NonEmptyStr
    evidence_report_id: NonEmptyStr
    granted_at: UtcDatetime
    valid_until: UtcDatetime
    signed_by: NonEmptyStr
    checksum: NonEmptyStr

    @classmethod
    def issue(cls, **kwargs: Any) -> Self:
        """Operator constructor. Computes checksum; agents must not call this."""
        payload = dict(kwargs)
        payload.setdefault("checksum", "0" * 64)
        return cls.model_validate(payload, context={"sign": True})

    def canonical_checksum(self) -> str:
        """Checksum over the signed fields of this instance."""
        return canonical_grant_checksum(self.model_dump(mode="json"))

    @model_validator(mode="after")
    def _c1_lifetime_and_checksum(self, info: ValidationInfo) -> Self:
        if self.valid_until <= self.granted_at:
            raise ValueError(
                "valid_until must be after granted_at; a grant without a "
                "positive lifetime cannot be consumed"
            )
        lifetime = self.valid_until - self.granted_at
        if lifetime > MAX_AUTHORITY_GRANT_LIFETIME:
            raise ValueError(
                "valid_until may not exceed granted_at by more than 90 days; "
                "there is no auto-renewal"
            )
        if len(set(self.allowed_actions)) != len(self.allowed_actions):
            raise ValueError("allowed_actions must be unique")
        if len(set(self.strategy_families)) != len(self.strategy_families):
            raise ValueError("strategy_families must be unique")
        if self.mode is AuthorityMode.BOUNDED:
            self._reject_live_bounded()
            self._reject_non_config_promotion()
        expected = self.canonical_checksum()
        context = info.context or {}
        if context.get("sign") is True:
            return self.model_copy(update={"checksum": expected})
        if self.checksum != expected:
            raise ValueError(
                "checksum does not match signed grant fields; the grant is "
                "not authentic"
            )
        return self

    def _reject_live_bounded(self) -> None:
        # C1 Stage A: reject LIVE BOUNDED rather than invent a second record type.
        if self.environment is Environment.LIVE:
            raise ValueError(
                "BOUNDED grants are rejected for Environment.LIVE; C1 is "
                "config-promotion only and LIVE BOUNDED requires a dedicated "
                "signed record that this contract does not model"
            )

    def _reject_non_config_promotion(self) -> None:
        if not self.allowed_actions:
            raise ValueError(
                "BOUNDED grants must list at least one config-promotion action"
            )
        if not self.strategy_families:
            raise ValueError(
                "BOUNDED grants must name at least one already-coded strategy family"
            )
        live_path = tuple(
            action.value for action in self.allowed_actions if action.is_live_path
        )
        if live_path:
            raise ValueError(
                "C1: BOUNDED grants cannot list live-path actions "
                f"{live_path}; only config-promotion FamilyStance proposals "
                "(enable/shadow/halt) are permitted"
            )
        non_promotion = tuple(
            action.value
            for action in self.allowed_actions
            if not action.is_config_promotion
        )
        if non_promotion:
            raise ValueError(
                "C1: BOUNDED grants may list only config-promotion actions; "
                f"rejected {non_promotion}"
            )
