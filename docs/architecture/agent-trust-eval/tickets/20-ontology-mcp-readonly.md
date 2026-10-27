# 20: ontology MCP read-only tools

**What to build:** When `UC_ONTOLOGY_MCP` is on, register read-only MCP tools (e.g. list / lookup) that expose the loader from ticket 19. When the flag is off, do not register tools (or soft-fail). Never provide write/remember/delete ontology tools. Fail soft on missing CONTEXT (empty list / not-found), not crash.

**Blocked by:** 19 (CONTEXT ontology loader + flag).

**Status:** ready-for-agent

- [ ] Flag on → list / lookup tools available and return loader data
- [ ] Flag off → tools absent or soft-fail; no hard dependency for workers
- [ ] No write-back / Evo / mutate APIs
- [ ] Missing CONTEXT → empty / not-found, not process crash
- [ ] Tests cover flag matrix without requiring a full coding agent
- [ ] Feature flag defaults off
