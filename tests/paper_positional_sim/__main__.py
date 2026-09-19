"""CLI: run the twelve positional PAPER lifecycle scenarios."""

from __future__ import annotations

import argparse
from pathlib import Path

from tests.paper_positional_sim.harness import ROOT
from tests.paper_positional_sim.report import write_report
from tests.paper_positional_sim.scenarios import run_all


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "tests" / "artifacts" / "paper_positional_sim",
    )
    args = parser.parse_args(argv)
    results = run_all(args.output / "work")
    report = write_report(
        results,
        args.output,
        commands=(
            "uv run python -m tests.paper_positional_sim",
            "uv run pytest tests/test_paper_positional_e2e_sim.py tests/test_paper_lifecycle.py tests/test_paper_review.py tests/test_paper_session.py -q",
        ),
        focused_tests="See pytest output in the PR checks.",
    )
    print(f"wrote {report}")
    for item in results:
        print(f"{item.case_id:02d} {item.label:7} {item.title}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
