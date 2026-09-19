#!/usr/bin/env python3
"""Realistic NIFTY breakout identification + MAE/MFE judgment (30 cells).

Usage:
    uv run python scripts/simulate_breakout_scenarios.py
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from trading.config.evaluation import load_evaluation_config
from trading.identification.breakout_scenarios import (
    format_breakout_report,
    persist_breakout_report,
    run_breakout_scenarios,
)
from trading.identification.config import load_identification_policy


def main() -> int:
    """Print the 30-cell judgment table and persist JSON under data/simulations/."""
    root = Path(__file__).resolve().parents[1]
    policy = load_identification_policy(root / "config" / "identification.yaml")
    evaluation = load_evaluation_config(root / "config" / "evaluation.yaml")
    rows = run_breakout_scenarios(policy, evaluation.config)
    print(format_breakout_report(rows))
    output = root / "data" / "simulations" / "breakout_scenarios.json"
    persist_breakout_report(rows, output, generated_at=datetime.now(tz=UTC))
    print(f"\nwrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
