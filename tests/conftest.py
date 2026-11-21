"""Shared pytest hooks for paper lifecycle modules on M2-only routing."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.fixtures.m2_veto_seed import remove_m2_veto_verdict, seed_m2_veto_approve
ROOT = Path(__file__).resolve().parent.parent

_M2_VETO_MODULES = frozenset(
    {
        "tests.test_paper_lifecycle",
        "tests.test_paper_runner",
        "tests.test_disc_a10_data_feed_error",
    }
)


@pytest.fixture(autouse=True)
def _seed_m2_veto_for_legacy_paper_tests(request: pytest.FixtureRequest) -> Iterator[None]:
    """M2-only routing blocks entries without a five-minute veto verdict on disk."""
    module = request.node.module.__name__
    if module not in _M2_VETO_MODULES:
        yield
        return
    if request.node.get_closest_marker("no_m2_veto_seed"):
        yield
        return
    clock = request.getfixturevalue("clock") if "clock" in request.fixturenames else None
    now = clock.now_utc() if clock is not None else None
    seed_m2_veto_approve(ROOT, now=now)
    yield
    remove_m2_veto_verdict(ROOT)