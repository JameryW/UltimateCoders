# 12: Hy-Memory optional auto context

**What to build:** When `UC_HY_MEMORY_AUTO_CONTEXT` is truthy, worker context assembly may recall Hy-Memory and inject into compile_context tiers (alongside existing search context). Default remains off (MCP-only). Missing package / MCP fails soft. No egress when compliance has not cleared the deployment — keep default off and document the compliance gate. Secrets never on argv.

**Blocked by:** None (supersede write is ticket 11 and should already hold whenever writes occur; not a hard code blocker for inject).

**Status:** implemented (branch `cursor/t12-hy-memory-auto-context-7c92`; `HY_MEMORY_AUTO_CONTEXT_ENV` / `hy_memory_auto_context_enabled` default off; `build_hy_memory_context_block` soft-skips; `from_hy_memory_block` → `Tier.MEMORY`; worker gateway + local seams append/compile; compliance: leave flag off until egress review)

- [x] Flag default off; MCP-only path unchanged when flag unset
- [x] Flag on + package available ⇒ recall appears in worker context assembly
- [x] Flag on + package missing ⇒ soft skip (no crash, no silent “success” claim that inject happened)
- [x] Does not delete or bypass supersede write rules from ticket 11
- [x] Tests cover flag off / on / missing-package at the worker context seam
- [x] Compliance note: leave flag off until egress review for that environment
