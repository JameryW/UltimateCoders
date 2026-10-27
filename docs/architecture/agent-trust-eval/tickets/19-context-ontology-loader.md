# 19: CONTEXT ontology loader + flag

**What to build:** Seed a minimal human-owned root CONTEXT authority file (or document soft-empty when absent) and a small loader module with `UC_ONTOLOGY_MCP` (default off). Parsing yields a stable term → definition (+ optional authority path) structure for MCP tools. Do **not** register MCP tools yet (ticket 20). Do not treat codegraph or Hy-Memory as glossary authority. No Agent write-back.

**Blocked by:** None.

**Status:** ready-for-agent

- [ ] `UC_ONTOLOGY_MCP` exists and defaults off
- [ ] Loader returns empty / soft-skip when flag off or CONTEXT absent
- [ ] Loader returns parsed terms when CONTEXT is present
- [ ] Codegraph / Hy-Memory are not used as glossary sources
- [ ] No ontology write / delete APIs
- [ ] Tests cover flag-off, missing file, and seeded parse without a live MCP server
- [ ] Feature flag defaults off
