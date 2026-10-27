# CONTEXT Ontology Loader Spec

> Human-owned root `CONTEXT.md` glossary loader gated by `UC_ONTOLOGY_MCP`
> (D9 / ticket 19). Default off. Soft-empty when flag off or file absent.
> MCP tool registration is ticket 20 — not this module.

---

## 1. Scope / Trigger

Modules: `python/ultimate_coders/agent/ontology.py`, root `CONTEXT.md`.

Trigger: any change to `UC_ONTOLOGY_MCP`, `OntologyTerm`, CONTEXT Markdown
parsing, or soft-empty behaviour. Do **not** register MCP tools here. Do
**not** read glossary content from codegraph or Hy-Memory. Do **not** add
write / delete / remember APIs. Do **not** enable the flag by default.

Tests: `tests/python/test_ontology_loader.py` (no live MCP server).

---

## 2. Signatures

```python
ONTOLOGY_MCP_ENV = "UC_ONTOLOGY_MCP"
DEFAULT_AUTHORITY_PATH = "CONTEXT.md"

@dataclass(frozen=True)
class OntologyTerm:
    name: str
    definition: str
    avoid: tuple[str, ...] = ()
    authority_path: str = "CONTEXT.md"

def ontology_mcp_enabled() -> bool: ...

def parse_context_markdown(
    text: str, *, authority_path: str = "CONTEXT.md"
) -> list[OntologyTerm]: ...

def load_ontology(
    *, path: str | Path | None = None, enabled: bool | None = None
) -> list[OntologyTerm]: ...

def list_terms(
    *, path: str | Path | None = None, enabled: bool | None = None
) -> list[OntologyTerm]: ...

def lookup_term(
    name: str,
    *,
    path: str | Path | None = None,
    enabled: bool | None = None,
    terms: Iterable[OntologyTerm] | None = None,
) -> OntologyTerm | None: ...
```

Truthy parser matches other UC flags: ``1`` / ``true`` / ``yes`` / ``on``
(case-insensitive). Unset / empty / other values → disabled.

---

## 3. Contracts

1. **Default off.** When `UC_ONTOLOGY_MCP` is unset or falsey,
   `load_ontology` / `list_terms` return `[]` and `lookup_term` returns
   `None` without reading CONTEXT.
2. **Soft empty.** Flag on + missing / unreadable CONTEXT → `[]` / `None`.
   Never raise for those soft cases.
3. **Authority.** Only human-owned CONTEXT Markdown (default repo-root
   `CONTEXT.md`, overridable `path` for tests). Codegraph and Hy-Memory are
   not glossary sources.
4. **Parse.** Matt Language format: `**Term**:` blocks with a one–two
   sentence definition and optional `_Avoid_:` synonym list. Case-insensitive
   lookup; canonical casing comes from the file. Malformed blocks are skipped.
5. **Read-only.** No `write_*`, `delete_*`, `remember_*`, or `save_*`
   ontology APIs in this module. Agent write-back / Evo is out of scope.
6. **No MCP registration.** T20 advertises `list` / `lookup` tools; T19 only
   exports the in-process structures those tools will call.
7. **Seed.** Root `CONTEXT.md` ships a minimal orchestration glossary
   (Gateway, Worker, Task, Subtask, Attempt, ExecutionEnvelope,
   CONTRACT_VERSION). Humans remain the only writers.

---

## 4. Validation & Error Matrix

| Condition | Required behaviour |
|---|---|
| Flag unset / falsey | `load_ontology` → `[]`; `lookup_term` → `None` |
| Flag on + missing CONTEXT | `[]` / `None` (soft empty) |
| Flag on + seeded CONTEXT / fixture | Parsed terms; lookup hits case-insensitively |
| Malformed term block | Skip that entry; do not crash |
| Write / delete API surface | Absent from module |

---

## 5. Good / Base / Bad Cases

- **Good:** flag on + fixture CONTEXT yields Gateway / Worker / etc.; lookup
  `"gateway"` returns canonical `Gateway`.
- **Base:** flag off keeps empty glossary and leaves `engine_mcp` unchanged.
- **Bad:** default-on; raise when CONTEXT absent; import Hy-Memory / codegraph
  for definitions; register MCP tools in T19; Agent write-back to CONTEXT.

---

## 6. Tests Required

| Contract | Test |
|---|---|
| Flag default off | `tests/python/test_ontology_loader.py` |
| Flag off / missing file soft empty | same file |
| Seeded parse + list/lookup | same file |
| No write API surface | same file |

---

## 7. Wrong vs Correct

#### Wrong

```python
# Always load; crash if CONTEXT missing; invent terms from Hy-Memory
terms = CodingMemory().recall("glossary")  # wrong authority
CONTEXT.write_text(llm_summary)            # Agent write-back
```

#### Correct

```python
if not ontology_mcp_enabled():
    return []
try:
    text = path.read_text(encoding="utf-8")
except OSError:
    return []
return parse_context_markdown(text)
```
