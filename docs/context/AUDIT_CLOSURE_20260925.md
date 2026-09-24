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

**VM:** `ubuntu@92.4.94.79` (`instance-20260912-0856`)  
**Deployed tree:** rsync from worktree `900293f` (no `.git` on VM).  
**Config checksums (sha256 prefix):** `charges.yaml:265d9da63cfa16c6`, `paper_session.yaml:3ea2dba6c6616d94`, `risk.yaml:060440d94533ff85`

| Check | Result | Evidence |
| --- | --- | --- |
| Code sync at candidate tree | **PASS** | `charges.yaml`, accounting modules present; config checksums match local 900293f worktree |
| `deploy_oracle.sh` bootstrap | **FAIL** | Remote `ruff check` reports 48 pre-existing violations; bootstrap aborted before full `pytest` on VM |
| VM product + audit pytest | **PASS** | 61 passed (`test_four_mode_session_integration`, `test_cas_event_path`, `test_audit_remediation`, `test_fill_charges`) |
| `new_entries_enabled: false` | **PASS** | `config/paper_session.yaml` on VM |
| `routing_profile: four_mode` | **PASS** | VM config |
| SHADOW/SUSPENDED M4 stances | **PASS** | `long_straddle`, `long_strangle` SHADOW; calendars SUSPENDED |
| `fno-automated.service` restart | **PASS** | `systemctl restart` → `active` |
| `fno-data-tick.service` | **PASS** | `active` |
| Legacy `trading.sqlite` in-place migration | **FAIL** | Opening store against pre-900293f DB raises `no such column: idempotency_key`; requires migration runbook before live session |
| Fresh DB schema (900293f) | **PASS** | New `fill_charges` table creates cleanly |
| Fyers provider ingress (`data fetch`) | **FAIL** | `FyersApiError 401: Please provide valid token` — refresh `.fyers_token` before market session |
| Live session router trace | **UNVERIFIED** | Off-hours; `fno-paper-session.service` inactive |
| Restart with entries disabled (live) | **UNVERIFIED** | Supervisor restarted; no open positions in restored DB path tested end-to-end during market hours |

**Operational notes**

- Pre-deploy `trading.sqlite` backed up on VM as `trading.sqlite.pre-900293f-*` and restored after schema probe.
- Entry promotion remains blocked: do **not** enable entries until legacy DB migration path is validated and Fyers token is refreshed.
- M4 SHADOW/SUSPENDED families must not be promoted in a config-only entry release.
