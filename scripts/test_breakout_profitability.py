#!/usr/bin/env python3
"""CLI script to test breakout profitability: Deterministic vs Agentic layers.

Runs at least 30 trades per scenario across Positional, Directional, and CAS,
testing genuine breakouts vs fake breakouts (bull/bear traps and auction spoofing).
Outputs detailed quantitative metrics and saves a structured JSON report.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trading.analytics.breakout_evaluation import (  # noqa: E402
    BreakoutStudyReport,
    ScenarioPerformanceSummary,
    run_full_study,
)

MIN_TRADES_PER_SCENARIO = 30


def _fmt_money(val: str | int | float) -> str:
    return f"₹{float(val):,.2f}"


def _print_scenario_table(summary: ScenarioPerformanceSummary) -> None:
    det = summary.to_dict()["deterministic"]
    agt = summary.to_dict()["agentic"]
    cf = summary.to_dict()["counterfactual"]

    kind = summary.scenario_kind.value
    print(f"\n{'=' * 78}")
    print(f" SCENARIO: {kind} ({summary.total_cases} Total Setups)")
    print(
        f" Genuine: {summary.genuine_count} | Fake Traps: {summary.fake_count}"
    )
    print(f"{'=' * 78}")
    print(f"{'Metric':<26} | {'Deterministic Layer':<22} | {'Agentic Desk':<22}")
    print(f"{'-' * 26}-+-{'-' * 22}-+-{'-' * 22}")

    print(f"{'Trades Executed':<26} | {det['trades']:<22} | {agt['trades']:<22}")
    print(f"{'Winning Trades':<26} | {det['wins']:<22} | {agt['wins']:<22}")
    print(f"{'Losing Trades':<26} | {det['losses']:<22} | {agt['losses']:<22}")

    det_wr_pct = f"{float(det['win_rate']) * 100:.1f}%"
    agt_wr_pct = f"{float(agt['win_rate']) * 100:.1f}%"
    print(f"{'Win Rate':<26} | {det_wr_pct:<22} | {agt_wr_pct:<22}")

    det_r = f"{float(det['total_r']):+.2f} R"
    agt_r = f"{float(agt['total_r']):+.2f} R"
    print(f"{'Net Return (in R)':<26} | {det_r:<22} | {agt_r:<22}")
    det_inr = _fmt_money(det["total_inr"])
    agt_inr = _fmt_money(agt["total_inr"])
    print(f"{'Net P&L (INR)':<26} | {det_inr:<22} | {agt_inr:<22}")
    det_pf = det['profit_factor']
    agt_pf = agt['profit_factor']
    print(f"{'Profit Factor':<26} | {det_pf:<22} | {agt_pf:<22}")
    det_dd = f"{det['max_drawdown_r']} R"
    agt_dd = f"{agt['max_drawdown_r']} R"
    print(f"{'Max Drawdown (R)':<26} | {det_dd:<22} | {agt_dd:<22}")

    print(f"{'-' * 26}-+-{'-' * 22}-+-{'-' * 22}")
    print(
        f" [Defense on Traps]: Vetoed = {agt['vetoes']} | "
        f"Downscaled = {agt['downscales']}"
    )
    print(
        f" [Saved Losses]    : +{cf['veto_saved_losses_r']} R (Veto) | "
        f"+{cf['downscale_saved_r']} R (Downscale)"
    )
    print(
        f" [Net Agent Alpha] : +{summary.net_alpha_r} R "
        f"({_fmt_money(summary.net_alpha_inr)})"
    )


def _print_overall_summary(report: BreakoutStudyReport) -> None:
    overall = report.overall
    det = overall.to_dict()["deterministic"]
    agt = overall.to_dict()["agentic"]
    cf = overall.to_dict()["counterfactual"]

    print(f"\n{'#' * 78}")
    print(
        f" OVERALL STUDY RESULTS ({overall.total_cases} Setups Across "
        "Positional, Directional, CAS)"
    )
    print(f"{'#' * 78}")

    det_wr = f"{float(det['win_rate']) * 100:.1f}%"
    agt_wr = f"{float(agt['win_rate']) * 100:.1f}%"
    det_r = f"{float(det['total_r']):+.2f} R"
    agt_r = f"{float(agt['total_r']):+.2f} R"
    det_dd = f"{det['max_drawdown_r']} R"
    agt_dd = f"{agt['max_drawdown_r']} R"

    print(f" {'Metric':<28} | {'Deterministic Layer':<20} | {'Agentic Desk':<20}")
    print(f" {'-' * 28}-+-{'-' * 20}-+-{'-' * 20}")
    print(f" {'Trades Executed':<28} | {det['trades']:<20} | {agt['trades']:<20}")
    print(f" {'Win Rate':<28} | {det_wr:<20} | {agt_wr:<20}")
    print(f" {'Total Return (in R)':<28} | {det_r:<20} | {agt_r:<20}")
    det_inr_str = _fmt_money(det['total_inr'])
    agt_inr_str = _fmt_money(agt['total_inr'])
    print(f" {'Total P&L (INR)':<28} | {det_inr_str:<20} | {agt_inr_str:<20}")
    det_pf_str = det['profit_factor']
    agt_pf_str = agt['profit_factor']
    print(f" {'Profit Factor':<28} | {det_pf_str:<20} | {agt_pf_str:<20}")
    print(f" {'Max Drawdown (R)':<28} | {det_dd:<20} | {agt_dd:<20}")

    veto_saved = cf['veto_saved_losses_r']
    down_saved = cf['downscale_saved_r']
    print(f"\n{'*' * 78}")
    print(" FAKE BREAKOUT & TRAP DEFENSE ANALYSIS:")
    print(f" - Total Fake Breakouts Encountered : {overall.fake_count}")
    print(f" - Trap Entries Blocked by Veto     : {agt['vetoes']} (100% loss avoided)")
    print(f" - Trap Entries Downscaled (0.3x)   : {agt['downscales']} (70% loss cut)")
    print(
        f" - Gross Capital Saved on Traps     : +{veto_saved} R (Veto) + "
        f"+{down_saved} R (Downscale)"
    )
    print(f" - Forfeited Gains on Winners       : -{cf['veto_missed_gains_r']} R")
    print(
        f" - NET AGENT ALPHA CONTRIBUTION     : +{overall.net_alpha_r} R "
        f"({_fmt_money(overall.net_alpha_inr)})"
    )
    print(f"{'*' * 78}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--trades-per-scenario",
        type=int,
        default=35,
        help="Number of trades per scenario (minimum 30, default 35)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed for data generation",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "data" / "simulations" / "breakout_profitability_report.json",
        help="Target JSON output path",
    )

    args = parser.parse_args(argv)

    if args.trades_per_scenario < MIN_TRADES_PER_SCENARIO:
        print("ERROR: --trades-per-scenario must be at least 30", file=sys.stderr)
        return 1

    print(
        f"Generating synthetic breakout market data "
        f"({args.trades_per_scenario} trades per scenario)..."
    )
    report = run_full_study(
        trades_per_scenario=args.trades_per_scenario, seed=args.seed
    )

    _print_scenario_table(report.positional)
    _print_scenario_table(report.directional)
    _print_scenario_table(report.cas)
    _print_overall_summary(report)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
    print(f"Full study results written to: {args.output}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
