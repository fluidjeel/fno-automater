"""Operator attention requests. Advisory only; never invokes live controls."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Protocol

from trading.config.evaluation import LoadedEvaluationConfig
from trading.domain.clock import Clock
from trading.domain.contracts import AttentionRequest
from trading.domain.enums import AttentionBlocker, ReasonCode
from trading.domain.ids import IdFactory

__all__ = [
    "AttentionSink",
    "MemoryAttentionSink",
    "scan_attention_blockers",
]


class AttentionSink(Protocol):
    """Where an attention request is recorded. Must not place orders."""

    def submit(self, request: AttentionRequest) -> None: ...


class MemoryAttentionSink:
    """In-memory sink for tests and CLI JSON output."""

    def __init__(self) -> None:
        self.requests: list[AttentionRequest] = []

    def submit(self, request: AttentionRequest) -> None:
        self.requests.append(request)


def scan_attention_blockers(
    *,
    clock: Clock,
    id_factory: IdFactory,
    evaluation: LoadedEvaluationConfig,
    cas_features_complete: bool,
    live_unverified_paths: tuple[str, ...],
    agent_enabled: bool,
    notify: AttentionSink | None = None,
    ttl: timedelta = timedelta(days=7),
) -> tuple[AttentionRequest, ...]:
    """Build structured blockers. Does not mutate live configuration."""
    now = clock.now_utc()
    requests: list[AttentionRequest] = []
    charges = evaluation.config.fill_model.charges_per_lot
    if not charges.is_verified:
        requests.append(
            _request(
                id_factory,
                now,
                ttl,
                blocker=AttentionBlocker.CHARGES_UNVERIFIED,
                detail=(
                    "evaluation fill_model.charges_per_lot is unverified; net "
                    "expectancy fails closed until a current broker/tax schedule "
                    "is recorded"
                ),
                artifact="verified charges_per_lot in config/evaluation.yaml",
            )
        )
    if not cas_features_complete:
        requests.append(
            _request(
                id_factory,
                now,
                ttl,
                blocker=AttentionBlocker.CAS_FEATURES_MISSING,
                detail=(
                    "cas-microstructure-v1 keys are incomplete; CAS stays halted "
                    "until Layer 1 emits all four features from depth/trade events"
                ),
                artifact="VALID cas-microstructure-v1 snapshots from live depth",
            )
        )
    if live_unverified_paths:
        requests.append(
            _request(
                id_factory,
                now,
                ttl,
                blocker=AttentionBlocker.LIVE_CONFIG_UNVERIFIED,
                detail=(
                    "LIVE market rules are unverified: "
                    + ", ".join(live_unverified_paths[:8])
                ),
                artifact="verified_at dates on config/base.yaml LIVE paths",
            )
        )
    if not agent_enabled:
        requests.append(
            _request(
                id_factory,
                now,
                ttl,
                blocker=AttentionBlocker.AGENT_DISABLED,
                detail=(
                    "weekly agent is disabled until paper evidence proves the "
                    "loop is worth paying for"
                ),
                artifact="set config/agent.yaml enabled true after paper evidence",
            )
        )
    sink = notify
    if sink is not None:
        for request in requests:
            sink.submit(request)
    return tuple(requests)


def telegram_attention_sink(
    send: Callable[[str], bool],
) -> AttentionSink:
    """Wrap a text sender. Failures are ignored; the request stays in memory."""

    class _TelegramSink:
        def submit(self, request: AttentionRequest) -> None:
            send(
                f"ATTENTION {request.blocker}: {request.detail}\n"
                f"Need: {request.required_artifact}"
            )

    return _TelegramSink()


def _request(
    id_factory: IdFactory,
    now: datetime,
    ttl: timedelta,
    *,
    blocker: AttentionBlocker,
    detail: str,
    artifact: str,
) -> AttentionRequest:
    return AttentionRequest(
        request_id=id_factory.new_id("ATTN"),
        blocker=blocker,
        reason_code=ReasonCode.OPERATOR_ATTENTION,
        detail=detail,
        required_artifact=artifact,
        as_of_time=now,
        valid_until=now + ttl,
    )
