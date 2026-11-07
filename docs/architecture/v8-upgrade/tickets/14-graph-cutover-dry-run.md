# 14 — Graph cutover dry-run readiness script

**What to build:** `scripts/check-graph-cutover-readiness.py` that prints a readiness checklist from the environment (and optional flags) without enabling shadow mode or editing Compose. Align `UC_GRAPH_SHADOW` parsing with gateway `resolve_graph_shadow_choice` (exact `"on"`). Add focused pytest coverage for pure helpers.

**Blocked by:** 13 (runbook path must exist so the script can point at it)

**Status:** ready

- [ ] Script exits 0 when no hard blockers; non-zero when configured inconsistently (e.g. shadow requested without DB URL) — define hard rules in the PRD/tests
- [ ] Never writes Compose / never sets `UC_GRAPH_SHADOW`
- [ ] Tests: `tests/python/test_graph_cutover_readiness.py`
