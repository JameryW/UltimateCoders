# T12: Hy-Memory optional auto context

## Source

- Ticket: `docs/architecture/agent-trust-eval/tickets/12-hy-memory-auto-context.md`
- Decision D6: default MCP-only; `UC_HY_MEMORY_AUTO_CONTEXT=1` enables worker inject
- Depends on T11 supersede invariant holding on any writes (not a hard code blocker)

## Goal

1. Flag helpers (prefer `hy_memory.py`):
   - `HY_MEMORY_AUTO_CONTEXT_ENV = "UC_HY_MEMORY_AUTO_CONTEXT"`
   - `hy_memory_auto_context_enabled()` — same truthy parser; **default off**
2. Worker context assembly (`worker.py` near search_block / `compile_context`):
   - When flag off: behaviour unchanged (MCP-only).
   - When flag on + `hy_memory_available()`: recall with a query derived from subtask description (limit small, e.g. 5); format a short block / `ContextItem`s (tier alongside search, e.g. via `from_search_block`-style helper or new `from_hy_memory_recall`); merge into `compile_context` items or append to context_block like search.
   - When flag on + package missing / recall fails: **soft skip** (no crash); do not claim inject succeeded in logs/events as hard success — optional debug reason ok.
3. Secrets stay allowlisted env; never argv. Do not bypass T11 supersede on remember. Compliance note in spec/docs: leave flag off until egress review.
4. Prefer extracting a small helper `build_hy_memory_context_block(query) -> str | None` testable without full Worker.

## Out of scope

Changing supersede write; replacing layered Memory; enabling flag by default; P2 tickets.

## Tests

`tests/python/test_hy_memory_auto_context.py`: flag default off; enabled+fake recall injects text; enabled+unavailable returns None / soft skip; worker seam or helper unit tests.
