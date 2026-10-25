# 11: Hy-Memory supersede-not-delete on write

**What to build:** On UC Hy-Memory remember / MCP remember paths, enforce supersede-not-delete: old facts are marked superseded rather than deleted. Independent of auto-inject (ticket 12). Keep package version pin; secrets stay in allowlisted env vars (never argv). Do not replace layered ShortTerm/LongTerm memory.

**Blocked by:** None.

**Status:** ready-for-agent

- [ ] Remember path marks prior facts superseded instead of deleting them
- [ ] Behaviour holds for both library wrapper and MCP remember tool
- [ ] Layered Memory modules remain the storage authority for short/long term
- [ ] Version pin / preflight behaviour unchanged or tightened, not loosened
- [ ] Tests cover supersede semantics at the CodingMemory / MCP seam
- [ ] No new auto-inject behaviour in this ticket (default remains MCP-only)
