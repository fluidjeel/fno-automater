"""Operator attention requests. Invariant 2: no live levers."""

from __future__ import annotations

from pathlib import Path

import tests.factories as f
from trading.cli import main
from trading.config import load_agent_config, load_config, load_evaluation_config
from trading.domain.clock import FrozenClock
from trading.domain.contracts import AttentionRequest
from trading.domain.enums import AttentionBlocker
from trading.domain.ids import SequentialIdFactory
from trading.ops.attention import (
    MemoryAttentionSink,
    scan_attention_blockers,
    telegram_attention_sink,
)

ROOT = Path(__file__).resolve().parent.parent


def test_attention_request_has_no_live_levers() -> None:
    forbidden = {
        "quantity",
        "lots",
        "contracts",
        "order",
        "stop",
        "broker",
        "promoted",
        "approved",
        "reservation",
        "deploy",
    }
    for name in AttentionRequest.model_fields:
        assert not forbidden & set(name.lower().split("_"))


def test_scan_flags_unverified_charges_and_cas_gap() -> None:
    clock = FrozenClock(f.NOW)
    sink = MemoryAttentionSink()
    requests = scan_attention_blockers(
        clock=clock,
        id_factory=SequentialIdFactory(clock.instant),
        evaluation=load_evaluation_config(ROOT / "config" / "evaluation.yaml"),
        cas_features_complete=False,
        live_unverified_paths=load_config(
            ROOT / "config" / "base.yaml"
        ).config.unverified_paths(),
        agent_enabled=load_agent_config(ROOT / "config" / "agent.yaml").config.enabled,
        notify=sink,
    )
    blockers = {item.blocker for item in requests}
    assert AttentionBlocker.CHARGES_UNVERIFIED not in blockers
    assert AttentionBlocker.CAS_FEATURES_MISSING in blockers
    assert AttentionBlocker.LIVE_CONFIG_UNVERIFIED in blockers
    assert AttentionBlocker.AGENT_DISABLED in blockers
    assert sink.requests == list(requests)


def test_cli_attention_scan_prints_blockers() -> None:
    assert main(["attention", "scan"]) == 0


def test_telegram_sink_is_advisory_text_only() -> None:
    sent: list[str] = []

    def _capture(text: str) -> bool:
        sent.append(text)
        return True

    sink = telegram_attention_sink(_capture)
    clock = FrozenClock(f.NOW)
    requests = scan_attention_blockers(
        clock=clock,
        id_factory=SequentialIdFactory(clock.instant),
        evaluation=load_evaluation_config(ROOT / "config" / "evaluation.yaml"),
        cas_features_complete=False,
        live_unverified_paths=(),
        agent_enabled=True,
        notify=sink,
    )
    assert requests
    assert sent
    assert "ATTENTION" in sent[0]
    assert "quantity" not in sent[0].lower()
