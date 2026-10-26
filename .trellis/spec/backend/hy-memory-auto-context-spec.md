# Hy-Memory Optional Auto-Context Spec

> Opt-in worker inject of Hy-Memory recall into context assembly (D6 / ticket 12).
> Default remains MCP-only. Independent of supersede-not-delete (ticket 11).

---

## 1. Scope / Trigger

Modules: `python/ultimate_coders/agent/hy_memory.py` (flag helpers +
`build_hy_memory_context_block`), `python/ultimate_coders/agent/context_compiler.py`
(`from_hy_memory_block`), `python/ultimate_coders/agent/worker.py` (gateway and
local context assembly).

Trigger: any change to `UC_HY_MEMORY_AUTO_CONTEXT`, auto-inject formatting, or
worker wiring that appends Hy-Memory beside search. Do **not** loosen T11
supersede-not-delete. Do **not** replace layered ShortTerm / LongTerm memory.
Do **not** enable the flag by default. Secrets stay in allowlisted env vars
(never argv).

**Compliance:** leave `UC_HY_MEMORY_AUTO_CONTEXT` off until egress review for
that environment. Enabling it may cause outbound LLM/embedder calls from the
worker process via the Hy-Memory SDK.

---

## 2. Signatures

```python
HY_MEMORY_AUTO_CONTEXT_ENV = "UC_HY_MEMORY_AUTO_CONTEXT"

def hy_memory_auto_context_enabled() -> bool: ...

def build_hy_memory_context_block(
    query: str,
    *,
    memory: CodingMemory | None = None,
    limit: int = 5,
) -> str | None: ...

def from_hy_memory_block(
    text: str | None, *, source: str = "hy-memory"
) -> list[ContextItem]:  # Tier.MEMORY (P4)
```

Truthy parser matches other UC flags: ``1`` / ``true`` / ``yes`` / ``on``
(case-insensitive). Unset / empty / other values → disabled.

---

## 3. Contracts

1. **Default off.** When the flag is unset or falsey, behaviour is unchanged
   (MCP-only). `build_hy_memory_context_block` returns `None` without recalling.
2. **Soft skip.** Flag on + package missing, construct failure, recall
   `ok=False`, empty channels, or empty query → return `None`. Never raise for
   a missing package. Do not claim inject succeeded in logs/events as a hard
   success (optional debug reason is fine).
3. **Format.** On non-empty recall, return a short readable block headed
   `## Coding memory` with fact / experience / chain lines.
4. **Compiler adapter.** `from_hy_memory_block` yields zero items for
   `None`/empty; otherwise one `ContextItem` at `Tier.MEMORY` (P4).
5. **Worker seams.** After search context:
   - Gateway path: append the hy block to `context_block` like search.
   - Local path: include `from_hy_memory_block(...)` in `compile_context` items
     alongside `from_search_block`; `render_legacy` emits MEMORY after evidence.
6. **T11 untouched.** Remember / MCP remember still supersede-not-delete; this
   ticket only reads via recall.
7. **Secrets.** `MEMORY_*` remain on the sandbox allowlist; the auto-context
   flag is a control-plane env var, never passed on argv.

---

## 4. Validation & Error Matrix

| Condition | Required behaviour |
|---|---|
| Flag unset / falsey | `build_hy_memory_context_block` → `None`; no recall |
| Flag on + fake memory with hits | Non-`None` block containing fact/experience text |
| Flag on + package unavailable | `None` (soft skip) |
| Flag on + recall `ok=False` / empty | `None` (soft skip) |
| `from_hy_memory_block("…")` | One item, `tier == Tier.MEMORY` |
| `from_hy_memory_block(None)` | `[]` |
| Worker gateway / local paths | Soft-skip when helper returns `None` |

---

## 5. Good / Base / Bad Cases

- **Good:** flag on + injected fake recall yields a coding-memory block; compiler
  places it at P4; worker appends / compiles without crash.
- **Base:** flag off keeps MCP-only and existing hy-memory / search tests green.
- **Bad:** default-on; crash when package missing; claiming inject success when
  soft-skipped; deleting facts or bypassing T11 supersede on remember.

---

## 6. Tests Required

| Contract | Test |
|---|---|
| Flag default off | `tests/python/test_hy_memory_auto_context.py` |
| Enabled + fake recall injects | same file |
| Enabled + unavailable → `None` | same file |
| `from_hy_memory_block` tier MEMORY | same file |
| Existing supersede / integration | `test_hy_memory_supersede.py`, `test_hy_memory_integration.py` stay green |

---

## 7. Wrong vs Correct

#### Wrong

```python
# Always inject, crash if package missing
hits = CodingMemory().recall(query)  # ImportError
context += format(hits)
```

#### Correct

```python
block = build_hy_memory_context_block(subtask.description)
# None when flag off / unavailable / empty — caller soft-skips
items = from_hy_memory_block(block)  # Tier.MEMORY or []
```
