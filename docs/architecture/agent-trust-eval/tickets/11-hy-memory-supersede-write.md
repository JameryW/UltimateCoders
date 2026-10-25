# 11: Hy-Memory supersede-not-delete on write

**What to build:** On UC Hy-Memory remember / MCP remember paths, enforce supersede-not-delete: old facts are marked superseded rather than deleted. Independent of auto-inject (ticket 12). Keep package version pin; secrets stay in allowlisted env vars (never argv). Do not replace layered ShortTerm/LongTerm memory.

**Blocked by:** None.

**Status:** implemented (branch `cursor/t11-hy-memory-supersede-7c92`; `CodingMemory.remember(supersede_fact_ids=...)` stamps `supersedes` + `supersede_policy=mark_not_delete`, returns `superseded_ids`; never calls delete/remove/drop; MCP `remember_coding_memory` forwards the arg; `HY_MEMORY_MIN_VERSION` / allowlist unchanged; no auto-inject)

- [x] Remember path marks prior facts superseded instead of deleting them
- [x] Behaviour holds for both library wrapper and MCP remember tool
- [x] Layered Memory modules remain the storage authority for short/long term
- [x] Version pin / preflight behaviour unchanged or tightened, not loosened
- [x] Tests cover supersede semantics at the CodingMemory / MCP seam
- [x] No new auto-inject behaviour in this ticket (default remains MCP-only)
