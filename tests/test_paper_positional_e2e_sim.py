"""Focused pytest wrapper around the positional PAPER e2e sim."""

from __future__ import annotations

from pathlib import Path

from tests.paper_positional_sim.report import write_report
from tests.paper_positional_sim.scenarios import run_all


def test_positional_paper_e2e_sim_writes_twelve_cases(tmp_path: Path) -> None:
    """Observational sim: twelve cases run; labels are evidence, not a green-wash."""
    output = tmp_path / "artifacts"
    results = run_all(output / "work")
    report = write_report(
        results,
        output,
        commands=("pytest tests/test_paper_positional_e2e_sim.py",),
        focused_tests="this test",
    )
    assert report.is_file()
    assert len(results) == 12
    assert {item.case_id for item in results} == set(range(1, 13))
    assert all(item.label in {"PASS", "FAIL", "UNKNOWN"} for item in results)
