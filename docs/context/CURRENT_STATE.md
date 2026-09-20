# Current State

LAST_UPDATED: 2026-09-20
CURRENT_MILESTONE: Agent Desk Stage E — research loop (E1–E2 DONE; E3 READY)
STATUS: P0_HARDENED_P1_SELECTION_UNATTENDED_NOT_LIVE_SAFE

## Confirmed decisions

- Binding C1: AuthorityMode.BOUNDED = config-promotion only; no LLM on live order path.
- Stage E research/advisory only; hypotheses never auto-implement; enter existing SHADOW ladder only.

## Implemented (Agent Desk)

- E1: RESEARCH weekly runner + playbook proposals + `data/agent_runs/` artifact.
- E2: ResearchHypothesis / ExperimentProposal; promote_improvements → existing
  SHADOW ExperimentDefinition (capital_limit=0); ineligible clusters stay notes.

## Verification

- `tests/test_research_weekly.py` + `tests/test_hypothesis_promotion.py` green.

## Blocking gaps

- 60s software-only poll cannot see intra-interval stop prints — not live-safe.
- LIVE market-rule values in base config remain unverified.
- CAS depth still needs a live 15:00–15:30 IST window for promotion evidence.

## Next action

ADESK-E3: Monthly meta-report (desk scorecards + demotions + det-vs-desk).

## Update rules

- Keep this file below 120 lines.
- Record facts with file/test evidence; do not paste logs or plans.
- Update only after a meaningful milestone or blocker change.
