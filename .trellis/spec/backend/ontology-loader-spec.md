# CONTEXT Ontology Loader Spec

> Human-owned root `CONTEXT.md` glossary loader gated by `UC_ONTOLOGY_MCP`
> (D9 / ticket 19). Default off. Soft-empty when flag off or file absent.
> Read-only MCP advertising lives in `engine_mcp._ontology_tools` (ticket 20).

---

## 1. Scope / Trigger

Modules: `python/ultimate_coders/agent/ontology.py`, root `CONTEXT.md`.
MCP splice (T20): `python/ultimate_coders/agent/engine_mcp.py`
(`_ontology_tools`, `list_ontology_terms`, `lookup_ontology_term`).

Trigger: any change to `UC_ONTOLOGY_MCP`, `OntologyTerm`, CONTEXT Markdown
parsing, soft-empty behaviour, or ontology MCP tool advertising. Do **not**
register MCP tools inside `ontology.py`. Do **not** read glossary content
from codegraph or Hy-Memory. Do **not** add write / delete / remember APIs.
Do **not** enable the flag by default.

Tests: `tests/python/test_ontology_loader.py`,
`tests/python/test_ontology_mcp.py` (helper-based; no live MCP server).

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
6. **MCP advertising (T20).** `engine_mcp._ontology_tools()` advertises
   `list_ontology_terms` / `lookup_ontology_term` only when
   `ontology_mcp_enabled()` and the optional `mcp` package are present.
   Handlers are thin wrappers over `list_terms` / `lookup_term` (soft empty /
   not-found text). When the flag is off, tools are **not** advertised.
   Never ship write / remember / delete ontology tools. `ontology.py` stays
   the sole authority API (no second parser).
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
| Write / delete API surface | Absent from loader module and MCP tool set |
| Flag off → MCP helper | `_ontology_tools()` → `[]` |
| Flag on + mcp present → MCP helper | tools `list_ontology_terms`, `lookup_ontology_term` |
| Flag on + missing CONTEXT (MCP) | soft empty / not-found text; no crash |

---

## 5. Good / Base / Bad Cases

- **Good:** flag on + fixture CONTEXT yields Gateway / Worker / etc.; lookup
  `"gateway"` returns canonical `Gateway`; MCP tools return the same loader
  data when advertised.
- **Base:** flag off keeps empty glossary and hides ontology MCP tools.
- **Bad:** default-on; raise when CONTEXT absent; import Hy-Memory / codegraph
  for definitions; always-register soft-fail dead tools; Agent write-back to
  CONTEXT; second Markdown parser inside `engine_mcp`.

---

## 6. Tests Required

| Contract | Test |
|---|---|
| Flag default off | `tests/python/test_ontology_loader.py` |
| Flag off / missing file soft empty | same file |
| Seeded parse + list/lookup | same file |
| No write API surface | same file |
| MCP flag matrix / hide when off | `tests/python/test_ontology_mcp.py` |
| MCP handlers soft empty + seeded | same file |
| No ontology write tool names | same file |

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

# T20 MCP: hide when off; wrap loader only
if not ontology_mcp_enabled() or Server is None:
    return []
return [Tool(name="list_ontology_terms", ...), Tool(name="lookup_ontology_term", ...)]
```
