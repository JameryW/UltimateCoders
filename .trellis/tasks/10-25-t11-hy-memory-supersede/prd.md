# T11: Hy-Memory supersede-not-delete on write

## Source

- Ticket: `docs/architecture/agent-trust-eval/tickets/11-hy-memory-supersede-write.md`
- Decision D6: supersede-not-delete hard invariant on write; independent of auto-inject (T12)

## Goal

1. **UC write invariant** on `CodingMemory.remember` (and MCP `remember_coding_memory`):
   - Never call a delete/tombstone API on the Hy-Memory client for “update” semantics.
   - Support supersede: optional `supersede_fact_ids: Sequence[str]` (and/or detect prior fact ids from metadata) → merge into outgoing `metadata` as e.g. `{"supersedes": [...], "supersede_policy": "mark_not_delete"}` and record superseded ids in the return payload (`superseded_ids`).
   - If the SDK exposes an update/mark API that is not delete, may use it; if not, metadata-only supersede is enough for T11 as long as delete is never invoked.
2. **Guard:** unit tests with a fake client assert `delete` / `remove` / `drop` are never called from remember; MCP path uses the same `CodingMemory.remember`.
3. Keep `HY_MEMORY_MIN_VERSION` / preflight / env allowlist unchanged or tightened; secrets never on argv.
4. Do **not** wire worker auto-inject (T12). Do not replace layered ShortTerm/LongTerm memory.

## Out of scope

`UC_HY_MEMORY_AUTO_CONTEXT` inject; compliance egress review; labeling/reviewer work.

## Tests

Extend `test_hy_memory_integration.py` or add `test_hy_memory_supersede.py`: remember with supersede_fact_ids stamps metadata; fake client never sees delete; MCP remember still goes through CodingMemory.
