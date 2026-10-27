# 19: CONTEXT ontology loader + flag

**What to build:** Seed a minimal human-owned root CONTEXT authority file (or document soft-empty when absent) and a small loader module with `UC_ONTOLOGY_MCP` (default off). Parsing yields a stable term → definition (+ optional authority path) structure for MCP tools. Do **not** register MCP tools yet (ticket 20). Do not treat codegraph or Hy-Memory as glossary authority. No Agent write-back.

**Blocked by:** None.

**Status:** **implemented** (`UC_ONTOLOGY_MCP` + `python/ultimate_coders/agent/ontology.py` + root `CONTEXT.md`; soft-empty; no MCP registration)

- [x] `UC_ONTOLOGY_MCP` exists and defaults off
- [x] Loader returns empty / soft-skip when flag off or CONTEXT absent
- [x] Loader returns parsed terms when CONTEXT is present
- [x] Codegraph / Hy-Memory are not used as glossary sources
- [x] No ontology write / delete APIs
- [x] Tests cover flag-off, missing file, and seeded parse without a live MCP server
- [x] Feature flag defaults off

## Implementation notes

- Module: `python/ultimate_coders/agent/ontology.py`
- Spec: `.trellis/spec/backend/ontology-loader-spec.md`
- Tests: `tests/python/test_ontology_loader.py`
- Seed glossary: root `CONTEXT.md` (Gateway, Worker, Task, Subtask, Attempt, ExecutionEnvelope, CONTRACT_VERSION)
- T20 wraps `list_terms` / `lookup_term` for MCP advertising; leave `engine_mcp.py` unchanged here
