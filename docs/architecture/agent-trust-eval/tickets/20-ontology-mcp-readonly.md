# 20: ontology MCP read-only tools

**What to build:** When `UC_ONTOLOGY_MCP` is on, register read-only MCP tools (e.g. list / lookup) that expose the loader from ticket 19. When the flag is off, do not register tools (or soft-fail). Never provide write/remember/delete ontology tools. Fail soft on missing CONTEXT (empty list / not-found), not crash.

**Blocked by:** none (was 19 — CONTEXT ontology loader + flag, now implemented).

**Status:** **implemented** (`UC_ONTOLOGY_MCP` + `engine_mcp._ontology_tools` → `list_ontology_terms` / `lookup_ontology_term`; hide when off; soft empty; no write tools)

- [x] Flag on → list / lookup tools available and return loader data
- [x] Flag off → tools absent or soft-fail; no hard dependency for workers
- [x] No write-back / Evo / mutate APIs
- [x] Missing CONTEXT → empty / not-found, not process crash
- [x] Tests cover flag matrix without requiring a full coding agent
- [x] Feature flag defaults off

## Landing notes

- Module splice: `python/ultimate_coders/agent/engine_mcp.py` (`_ontology_tools`, handlers)
- Authority API unchanged: `python/ultimate_coders/agent/ontology.py` (`list_terms` / `lookup_term`)
- Tests: `tests/python/test_ontology_mcp.py` (helper-based; no stdio MCP server)
- Spec: `.trellis/spec/backend/ontology-loader-spec.md` §6 MCP advertising note
