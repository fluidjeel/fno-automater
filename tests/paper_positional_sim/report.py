"""Write the positional PAPER sim report artifacts."""

from __future__ import annotations

from pathlib import Path

from tests.paper_positional_sim.scenarios import CaseResult

GAP_CASES = {7, 5}


def write_report(
    results: list[CaseResult],
    output_dir: Path,
    *,
    commands: tuple[str, ...],
    focused_tests: str,
) -> Path:
    """Write REPORT.md plus per-case traces. Returns the report path."""
    output_dir.mkdir(parents=True, exist_ok=True)
    lines: list[str] = [
        "# PAPER positional lifecycle simulation",
        "",
        "Stacked on PR #3 (lifecycle recovery) and PR #4 (twice-daily review).",
        "Production session, strategy, Layer 2, TradeManager, OMS and paper broker",
        "are used as-is. Only the clock, quotes and broker acknowledgements are fixtures.",
        "",
        "## 1. Case table",
        "",
        "| Case | Label | Realized result | Evidence |",
        "| --- | --- | --- | --- |",
    ]
    for item in results:
        item.evidence = str(output_dir / f"case-{item.case_id:02d}")
        realized = item.realized.replace("|", "\\|").replace("\n", " ")
        lines.append(
            f"| {item.case_id}. {item.title} | **{item.label}** | {realized} | `{item.evidence}` |"
        )
    lines.extend(
        [
            "",
            "## 2. Full chronological traces (overnight gap and UNKNOWN exit restart)",
            "",
        ]
    )
    for item in results:
        if item.case_id not in GAP_CASES:
            continue
        lines.append(f"### Case {item.case_id}: {item.title}")
        lines.append("")
        lines.append("```")
        lines.append(item.trace_text or "(empty trace)")
        lines.append("```")
        lines.append("")
    first_fail = next((item for item in results if item.label == "FAIL"), None)
    lines.extend(["## 3. Findings", "", "### Safety defects", ""])
    defects = [item for item in results if item.label == "FAIL"]
    if not defects:
        lines.append("None demonstrated as invariant violations in this run.")
    else:
        for item in defects:
            lines.append(
                f"- **Case {item.case_id}** `{item.first_fail}`: {item.realized}"
            )
            if item.proposed_fix:
                lines.append(f"  Proposed correction: {item.proposed_fix}")
    lines.extend(["", "### Paper-model limitations", ""])
    lines.extend(
        [
            "- PAPER protective STOPs remain local software stubs, not broker-resident working orders.",
            "- Case 8 PASS is a limitation detected: `poll_interval_seconds=60` cannot see a stop that prints and reverses between polls. SAFETY: NOT ACCEPTABLE for live unattended stops.",
            "- Conservative fills require a published PaperBroker quote. This "
            "harness publishes each observation as the live book; production "
            "`_publish_quotes` only runs while evaluating a new intent.",
            "- Charges are itemized from `config/evaluation.yaml` round-trip "
            "`charges_per_lot`, not a live contract note.",
        ]
    )
    lines.extend(["", "### Review-policy gaps", ""])
    lines.extend(
        [
            "- Production `positional_long_option` / `debit_spread` freeze `break_even_trigger_ticks=None` and no trail (case 3 PASS: disabled template). Case 4 overlays BE/trail in the sim harness only.",
            "- ReviewEngine does not consume IV, theta, quoted spread or margin; those inputs HOLD unless a frozen stop/target/expiry rule fires.",
            "- `PROPOSE_HEDGE` / `PROPOSE_ROLL` persist `REVIEW_PROPOSAL_REQUIRES_L2` and never auto-submit.",
        ]
    )
    if first_fail is not None:
        lines.extend(
            [
                "",
                "## First FAIL (no production patch in this run)",
                "",
                f"- Call site: `{first_fail.first_fail}`",
                f"- Result: {first_fail.realized}",
                f"- Smallest proposed correction: {first_fail.proposed_fix}",
            ]
        )
    lines.extend(
        [
            "",
            "## 4. Commands and focused tests",
            "",
        ]
    )
    for command in commands:
        lines.append(f"- `{command}`")
    lines.append("")
    lines.append(focused_tests)
    lines.extend(
        [
            "",
            "## 5. Verdict",
            "",
            _verdict(results),
            "",
        ]
    )
    report = output_dir / "REPORT.md"
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    for item in results:
        case_dir = output_dir / f"case-{item.case_id:02d}"
        case_dir.mkdir(parents=True, exist_ok=True)
        (case_dir / "trace.txt").write_text(item.trace_text + "\n", encoding="utf-8")
        (case_dir / "summary.txt").write_text(
            f"{item.label}\n{item.realized}\n{item.first_fail or ''}\n",
            encoding="utf-8",
        )
    return report


def _verdict(results: list[CaseResult]) -> str:
    fails = [item for item in results if item.label == "FAIL"]
    unknown = [item.case_id for item in results if item.label == "UNKNOWN"]
    if fails:
        first = fails[0]
        return (
            "Not suitable for unattended forward observation until the first FAIL is "
            f"fixed: case {first.case_id} at `{first.first_fail}`. "
            "LIVE promotion remains blocked by that defect plus software-only stops, "
            "the 60s poll gap, unreachable trail/partial review actions, and "
            "PAPER-005 evidence/LIVE-config requirements."
        )
    if unknown:
        return (
            "Suitable for attended PAPER observation of the implemented HOLD/stop/"
            "restart path, not for unattended LIVE promotion. UNKNOWN coverage "
            f"on cases {unknown} (mostly trail/partial/IV inputs the frozen template "
            "does not use). Remaining LIVE blockers: software-only stops, 60s poll "
            "gap, no auto hedge/roll, and PAPER-005."
        )
    return (
        "PAPER positional lifecycle is suitable for attended forward observation "
        "of entry, frozen-policy stops/targets, twice-daily HOLD reviews, 15:40 "
        "persist/restore and missed-slot catch-up. It is not unattended-PAPER or "
        "LIVE-ready: software-only protection, case 8 60s poll gap (SAFETY: NOT "
        "ACCEPTABLE for live unattended stops), and hedge/roll still require a "
        "new Layer 2 trade. PAPER-005 stays blocked."
    )
