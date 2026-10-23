# ACTIVE_PLAN — M2-only branch and Oracle cutover

PLAN_STATUS: APPROVED (operator request 2026-10-23)
CONTEXT_DIGEST_VERSION: 1
CONTEXT_REFRESH_REQUIRED: no

## Goal

Single git branch **`M2`** deployed to Oracle: PAPER runs **M2_DIRECTIONAL only**; M1/M3/M4
disabled in config and purged from Oracle paper state.

## Scope

- `routing_profile: m2_only` + configs (`paper_session`, `modes`, `discovery`) M2-only.
- Mode book ledgers built from **`modes.yaml` keys** (not hard-coded four modes).
- Startup/post-open checks for M2-only deployment.
- Script `scripts/oracle_m2_only_cleanup.py` + Oracle execution (flatten non-M2 opens, purge history).
- Push branch `M2`, pull on VM, cleanup, restart paper stack.

## Non-goals

- Delete M1/M3/M4 strategy source trees (enums/replay stay; modes not routed).
- LIVE broker changes.
- Merge to `main` in this pass.

## Acceptance

- `load_paper_session_config` → `m2_only`, only `M2_DIRECTIONAL: PAPER`.
- `pytest tests/test_startup_validation.py tests/test_paper_session.py::TestShippedSession -q` pass.
- Oracle: no open non-M2 lifecycles; paper session starts with M2-only config.

## Residual risks

- Historical analytics scripts assuming four-mode book need mode filters.
- Full test suite may still reference four-mode fixtures (CI subset above).
