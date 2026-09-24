# Audit closure — four_mode_20260925 + m3_m4_20260925

## Candidate tree

| Field | Value |
| --- | --- |
| Commit | `900293fd11364549bb8a02dbb174de770f4ff672` |
| Tree | `6cf3ed5ffb8baff49c4895af02d8e2b1ed6ebde9` |
| Tag | `audit-candidate-20260925` |
| Branch | `main` (8 commits ahead of `origin/main` at closure) |

### Predecessor commits (remediation stack)

1. `a85d64c` — entry legs + fill-ledger gross P&L
2. `720b409` — ₹35k global/mode open-risk caps
3. `dce5a77` — M3/M4 following-week expiries
4. `ed960bc` — M1 event path + M2 carry market state
5. `8d21f73` — per-leg roll/switch + L2 replacement
6. `6347679` — campaign drawdown persistence
7. `900293f` — durable per-fill charges + conservative net

## Gate 1 — clean-tree audit closure

**Date:** 2026-09-25  
**Worktree:** only `docs/plans/ACTIVE_PLAN.md` intentionally unstaged after closure commit.

### Test evidence

| Suite | Command scope | Result |
| --- | --- | --- |
| Full | `uv run pytest -o addopts=''` | **1831 passed**, 5 skipped |
| Product | `test_four_mode_session_integration`, `test_four_mode_trade_simulations`, `test_cas_event_path`, `test_m1_paper_session_integration` | **21 passed** |
| Audit (four_mode) | `test_audit_remediation` | **47 passed** |
| Audit (charges) | `test_fill_charges` | **8 passed** |
| Product + audit combined | all above | **79 passed** |

Audit regressions are positive product assertions (no defect-expecting checks).

## PASS / FAIL / UNVERIFIED by mode

| Mode | Scope | Status | Evidence |
| --- | --- | --- | --- |
| **M1** | CAS event path, stale-quote reject, poll vs event submit, restart | **PASS** | `test_cas_event_path`, `test_m1_paper_session_integration`, `TestP0M1EventPathAndM2Carry` |
| **M2** | Carry gate on session path, directional fills, neutral abstain | **PASS** | `test_m2_sim_*`, `TestP0M1EventPathAndM2Carry::test_m2_carry_gate_approves_on_session_path` |
| **M3** | Debit/credit fills, duplicate suppression, exit sequencing, P&L ledger, roll/switch, campaign, charges | **PASS** | `test_m3_sim_*`, `TestP0LiabilityFirstExitSequencing`, `TestP0ClosedMultilegPreservesEntryFillsAndPnl`, `TestP0RollSwitch*`, `TestP0CampaignDrawdownRollChain`, `test_fill_charges` |
| **M4** | Iron condor + call butterfly lifecycle (tested families) | **PASS** | `test_m4_sim_1/2`, audit structure-close parametrized tests |
| **All** | AI cannot block exit, entry hold, open-risk caps, restart ledger | **PASS** | `TestP0AiCannotBlockDeterministicExit`, `TestP0EntryHold*`, `TestP0OpenRiskCaps*` |
| **All** | Live Oracle session router trace during market hours | **UNVERIFIED** | Requires Gate 2 on VM |
| **All** | Live Fyers provider ingress + measured latency during session | **UNVERIFIED** | Requires Gate 2 `--fetch` / market session |

## PASS / FAIL / UNVERIFIED — M4 families

| Family | Stance (`paper_session.yaml`) | Lifecycle evidence | Status |
| --- | --- | --- | --- |
| `short_iron_condor_defined` | PAPER | Entry/exit/roll/close + charges | **PASS** |
| `long_call_butterfly` | PAPER | Entry/exit/roll/close + charges | **PASS** |
| `bull_call_debit` | PAPER (M3) | Full multileg close + restart P&L | **PASS** |
| `bull_put_credit` | PAPER (M3) | Full multileg close + restart P&L | **PASS** |
| `long_straddle` | SHADOW | No family-level lifecycle suite | **UNVERIFIED** — do not promote |
| `long_strangle` | SHADOW | No family-level lifecycle suite | **UNVERIFIED** — do not promote |
| `long_call_calendar` | SUSPENDED | No family-level lifecycle suite | **UNVERIFIED** — do not promote |
| `long_put_calendar` | SUSPENDED | No family-level lifecycle suite | **UNVERIFIED** — do not promote |

## Configuration hold

- `config/paper_session.yaml`: `new_entries_enabled: false` (unchanged at candidate commit).
- SHADOW/SUSPENDED M4 families must not be auto-promoted when entries are re-enabled.

## Gate 2 — Oracle deployment verification

See deployment section appended after VM run completes.
