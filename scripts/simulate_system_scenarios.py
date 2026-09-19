#!/usr/bin/env python3
"""Offline paper identification matrix: force a family instead of abstain.

Usage:
    uv run python scripts/simulate_system_scenarios.py
"""

from __future__ import annotations

from pathlib import Path

from trading.identification.config import load_identification_policy
from trading.identification.paper_scenarios import (
    format_matrix,
    run_paper_scenario_matrix,
)


def main() -> int:
    """Print the 15-cell matrix and return 1 if the desk still abstains a lot."""
    root = Path(__file__).resolve().parents[1]
    policy = load_identification_policy(root / "config" / "identification.yaml")
    rows = run_paper_scenario_matrix(policy)
    print(format_matrix(rows))
    abstain = sum(1 for row in rows if row.paper_winner is None)
    # Paper fail-fast should name a family on every cell of this matrix.
    return 0 if abstain == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
