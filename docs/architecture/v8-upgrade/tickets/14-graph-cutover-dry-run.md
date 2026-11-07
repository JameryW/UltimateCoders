# 14 — Graph cutover dry-run readiness script

**What to build:** `scripts/check-graph-cutover-readiness.py` that prints a readiness checklist from the environment (and optional flags) without enabling shadow mode or editing Compose. Align `UC_GRAPH_SHADOW` parsing with gateway `resolve_graph_shadow_choice` (exact `"on"`). Add focused pytest coverage for pure helpers.

**Blocked by:** 13 (runbook path must exist so the script can point at it)

**Status:** implemented (branch `cursor/v8-graph-cutover-runbook-e6a9`; `scripts/check-graph-cutover-readiness.py` + `tests/python/test_graph_cutover_readiness.py`; exact `on` gate; hard-fail shadow-on without `UC_DATABASE_URL`; never sets env / never mutates Compose)

- [x] Script exits 0 when no hard blockers; non-zero when configured inconsistently (e.g. shadow requested without DB URL) — define hard rules in the PRD/tests
- [x] Never writes Compose / never sets `UC_GRAPH_SHADOW`
- [x] Tests: `tests/python/test_graph_cutover_readiness.py`
