"""Seed an APPROVE M2 veto verdict for legacy paper cycle tests (M2-only routing)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from trading.trade.m2_veto import (
    M2VetoAction,
    M2VetoVerdict,
    load_m2_veto_config,
    write_verdict,
)

__all__ = ["remove_m2_veto_verdict", "seed_m2_veto_approve"]


def seed_m2_veto_approve(
    repo_root: Path,
    *,
    now: datetime | None = None,
    valid_for_seconds: int | None = None,
) -> Path:
    """Write a fresh APPROVE verdict so ``admit_m2_entry`` does not block tests."""
    config = load_m2_veto_config(repo_root / "config" / "m2_veto.yaml")
    as_of = now or datetime.now(tz=UTC)
    ttl = valid_for_seconds if valid_for_seconds is not None else config.valid_for_seconds
    verdict = M2VetoVerdict(
        action=M2VetoAction.APPROVE,
        size_fraction=Decimal("1"),
        reasons=("test fixture approve",),
        as_of=as_of,
        valid_until=as_of + timedelta(seconds=ttl),
        source="test-fixture",
    )
    write_verdict(verdict, config, repo_root=repo_root)
    return repo_root / config.verdict_path


def remove_m2_veto_verdict(repo_root: Path) -> None:
    """Remove the seeded verdict file when present."""
    config = load_m2_veto_config(repo_root / "config" / "m2_veto.yaml")
    path = repo_root / config.verdict_path
    if path.is_file():
        path.unlink()