# Hy-Memory Supersede-Not-Delete Spec

> UC write invariant for Hy-Memory remember paths (D6 / ticket 11): mark prior
> facts superseded; never delete. Independent of auto-inject (ticket 12).

---

## 1. Scope / Trigger

Modules: `python/ultimate_coders/agent/hy_memory.py` (`CodingMemory.remember`)
and `python/ultimate_coders/agent/engine_mcp.py` (`remember_coding_memory`).

Trigger: any change to remember metadata stamping, supersede argument plumbing,
or MCP remember tool schema. Do **not** call Hy-Memory client delete / remove /
drop / tombstone APIs for “update” semantics. Do **not** wire
`UC_HY_MEMORY_AUTO_CONTEXT` inject here (ticket 12). Do **not** replace layered
ShortTerm / LongTerm memory. Keep `HY_MEMORY_MIN_VERSION`, preflight, and the
`MEMORY_*` env allowlist unchanged or tightened — secrets never on argv.

---

## 2. Signatures

```python
SUPERSEDES_METADATA_KEY = "supersedes"
SUPERSEDE_POLICY_METADATA_KEY = "supersede_policy"
SUPERSEDE_POLICY_MARK_NOT_DELETE = "mark_not_delete"

class CodingMemory:
    def remember(
        self,
        messages: list[dict[str, Any]],
        *,
        metadata: dict[str, Any] | None = None,
        supersede_fact_ids: Sequence[str] | None = None,
    ) -> dict[str, Any]: ...
```

MCP tool `remember_coding_memory` accepts optional `supersede_fact_ids: string[]`
and forwards it to `CodingMemory.remember` — same invariant, no parallel write path.

---

## 3. Contracts

1. **Add-only write.** `remember` may call `client.add` (or a non-delete update
   API if the SDK clearly exposes one). It must never call delete / remove /
   drop / tombstone (or equivalently named) client methods for supersede.
2. **Metadata stamp.** When `supersede_fact_ids` is non-empty after normalisation,
   or when caller `metadata` already lists a `supersedes` sequence, outgoing
   metadata includes:
   - `supersedes`: de-duplicated list of non-empty string ids (union of prior
     metadata ids and the explicit arg)
   - `supersede_policy`: always `"mark_not_delete"`
3. **Caller keys preserved.** Unrelated metadata keys are shallow-copied; the
   merge must not wipe them. When there is nothing to supersede, pass the
   original `metadata` through unchanged (including `None`).
4. **Return payload.** When ids were marked, the return dict includes
   `superseded_ids` equal to the stamped `supersedes` list. SDK fields
   (`success`, `fact_ids`, …) remain present on a dict copy of the add result.
5. **MCP parity.** `remember_coding_memory` is the only MCP write entry; it
   uses `CodingMemory.remember` with the same optional `supersede_fact_ids`.
   No worker auto-inject in this ticket.
6. **Version / allowlist.** `HY_MEMORY_MIN_VERSION` (`2.0.0`), preflight, and
   `HY_MEMORY_ENV_ALLOWLIST` stay unchanged or tightened. Documented package
   pin remains `hy-memory==2.0.2`. Secrets stay in allowlisted env vars.
7. **Layered memory untouched.** ShortTerm / LongTerm remain the KV / semantic
   storage authority; Hy-Memory stays the cognitive extract backend.

---

## 4. Validation & Error Matrix

| Condition | Required behaviour |
|---|---|
| `supersede_fact_ids=["f-old"]` | `add` metadata has `supersedes` + `mark_not_delete`; return has `superseded_ids` |
| metadata already has `supersedes` | Same stamp + return; union with arg if both present |
| no supersede intent | metadata passed through; no `superseded_ids` key required |
| fake client with `delete`/`remove`/`drop` | never invoked from `remember` |
| MCP remember with `supersede_fact_ids` | reaches `CodingMemory.remember`; same metadata stamp |
| SDK / endpoints down | existing remember/`success` False behaviour; no delete fallback |

---

## 5. Good / Base / Bad Cases

- **Good:** remember with supersede stamps metadata and returns `superseded_ids`;
  delete methods on a recording fake stay at zero calls; MCP forwards the arg.
- **Base:** remember without supersede keeps prior scene / coordinate / source
  contracts and existing integration tests green.
- **Bad:** “update” implemented by deleting the old fact; MCP invents a second
  write path that bypasses `CodingMemory`; auto-inject wired in this change.

---

## 6. Tests Required

| Contract | Test |
|---|---|
| Metadata stamp + return | `tests/python/test_hy_memory_supersede.py` |
| Never delete / remove / drop | same file, recording fake |
| MCP passes `supersede_fact_ids` | same file (handler path) |
| Existing hy-memory contracts | `tests/python/test_hy_memory_integration.py` stays green |

---

## 7. Wrong vs Correct

#### Wrong

```python
client.delete(old_id)
client.add(messages, ...)
```

#### Correct

```python
mem.remember(
    messages,
    supersede_fact_ids=["fact-old"],
    metadata={"task_id": "t-1"},
)
# -> client.add(..., metadata={"task_id": "t-1",
#       "supersedes": ["fact-old"],
#       "supersede_policy": "mark_not_delete"})
# -> {"success": True, ..., "superseded_ids": ["fact-old"]}
```
