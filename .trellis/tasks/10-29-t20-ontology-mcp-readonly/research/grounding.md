# T20 grounding — ontology MCP read-only tools

Authority inputs: [D9](../../../../docs/architecture/agent-trust-eval/issues/D9-project-ontology-mcp.md), [ticket 20](../../../../docs/architecture/agent-trust-eval/tickets/20-ontology-mcp-readonly.md), [spec-p3](../../../../docs/architecture/agent-trust-eval/spec-p3.md) Seam I, [ontology-loader-spec](../../../spec/backend/ontology-loader-spec.md), T19 landing (`ontology.py` + root `CONTEXT.md`). Surveyed on `cursor/t20-ontology-mcp-readonly-7c92` (2026-10-29).

Thoroughness: medium. Goal: pin how T20 wraps T19’s loader into `engine_mcp` without inventing authority, write APIs, or a full MCP server in tests.

---

## 1. T19 surface ready for wrapping

`python/ultimate_coders/agent/ontology.py` already exports:

| Symbol | Contract |
|--------|----------|
| `ONTOLOGY_MCP_ENV` / `ontology_mcp_enabled()` | `UC_ONTOLOGY_MCP`; truthy `1/true/yes/on`; **default off** |
| `OntologyTerm` | `name`, `definition`, `avoid`, `authority_path` |
| `load_ontology` / `list_terms` | Soft `[]` when flag off or CONTEXT missing/unreadable |
| `lookup_term` | Case-insensitive; soft `None` when disabled / missing / not found |

Module docstring explicitly defers MCP registration to ticket 20. Root `CONTEXT.md` seeds seven orchestration terms. Spec: `.trellis/spec/backend/ontology-loader-spec.md` §6 — **No MCP registration** in T19.

**Implication:** T20 must call these helpers, not re-parse Markdown or invent a parallel schema.

---

## 2. `engine_mcp.py` registration patterns (peers)

Conditional advertising already exists:

| Helper | Gate | Tools |
|--------|------|-------|
| `_memory_tools()` | `hy_memory_available()` and `Server is not None` | `recall_coding_memory`, `remember_coding_memory` |
| `_deepseek_web_tools()` | `deepseek_web_configured()` and `Server is not None` | `deepseek_web_search`, `deepseek_web_fetch` |
| always-on block in `list_tools` | MCP installed | `search_code`, `read_memory`, `write_memory` |

`_create_server` splices optional lists into `list_tools()` and dispatches in `call_tool`. Optional `mcp` import → `Server`/`Tool`/`TextContent` are `None`; helpers return `[]` so callers never `NameError`.

Unit tests call helpers **without** a live stdio server:

- `tests/python/test_engine_mcp_deepseek_web.py` → `_deepseek_web_tools()`
- `tests/python/test_hy_memory_integration.py` → `_memory_tools()` (+ handler calls when `mcp` present)

**Implication for T20:** Add `_ontology_tools()` (or equivalent) gated on `ontology_mcp_enabled()` + `Server is not None`; splice into `list_tools`; add thin `call_tool` branches. Prefer testing the helper (and optional handler text) over spinning a fake `Server`.

---

## 3. Ticket 20 / D9 / spec-p3 acceptance matrix

From ticket 20 + D9 + spec-p3 Seam I:

| Condition | Required |
|-----------|----------|
| Flag off (default) | Ontology tools **absent** from advertised list (preferred over “register then soft-fail”) |
| Flag on + missing CONTEXT | Soft empty / not-found — **no process crash** |
| Flag on + seeded CONTEXT | `list` / `lookup` return loader data |
| Write / remember / delete ontology tools | **Never** |
| Tests | Flag matrix without a full coding agent |

D9 wording allows “不注册或注册后软失败”; ticket + operator story prefer **hide when off**. Soft-empty remains the runtime contract when flag is on but CONTEXT is gone (loader already does this).

---

## 4. Recommended tool shape (Approach A)

**Names (preferred):** `list_ontology_terms`, `lookup_ontology_term`  
(Avoid colliding with bare `list_terms` / in-process APIs; parallel to `recall_coding_memory` style.)

**Thin wrappers:**

```text
_ontology_tools() -> list[Tool]
  if not ontology_mcp_enabled() or Server is None: return []
  return [Tool(list_ontology_terms...), Tool(lookup_ontology_term...)]

_list_ontology_terms(args) -> TextContent
  terms = list_terms()   # respects flag + soft empty
  # render name / definition / authority_path (+ optional avoid)

_lookup_ontology_term(args) -> TextContent
  term = lookup_term(args["name"])
  # not found → soft message, not exception
```

**Schemas (MVP):**

- `list_ontology_terms`: no required args (optional future: `path` for tests only — prefer env/CWD path in production; keep schema empty for MVP).
- `lookup_ontology_term`: required `name: string`.

**Response:** plain text lines (same family as search/memory MCP), not a second JSON protocol. Include `authority_path` so agents see CONTEXT as authority.

**Hard rules:**

- Do not add `write_ontology_*`, `remember_*`, `delete_*`, or mutate CONTEXT.
- Do not load terms from codegraph or Hy-Memory.
- Do not change `ontology.py` contracts except trivial export needs; prefer all MCP glue in `engine_mcp.py`.

---

## 5. Feasible approaches (for PRD)

**A. Thin `_ontology_tools` in `engine_mcp` (Recommended)**  
Gate on `UC_ONTOLOGY_MCP`; hide when off; wrap `list_terms` / `lookup_term`; unit-test helper (+ optional handlers). Pros: matches DeepSeek/Hy-Memory peers; reuses T19 authority; smallest diff. Cons: couples ontology advertising to engine MCP process (acceptable — same as other UC tools).

**B. Separate `ontology_mcp.py` stdio server**  
Standalone MCP entry. Pros: clearer module boundary. Cons: second process/config for operators; duplicates Server/Tool boilerplate; ticket expects engine MCP registration.

**C. Always register tools; soft-fail inside handlers**  
Tools always listed; handlers return empty when flag off. Pros: simpler list_tools. Cons: weaker default-off story vs ticket “tools absent”; agents see dead tools when flag unset.

---

## 6. Test plan (no full MCP / no coding agent)

New file (suggested): `tests/python/test_engine_mcp_ontology.py`

| Case | Assert |
|------|--------|
| Flag unset / falsey | `_ontology_tools()` → `[]` |
| Flag on + `Server` present | tool names `list_ontology_terms`, `lookup_ontology_term` |
| Flag on + `Server is None` | `[]` (optional mcp missing) |
| No write tool names | advertised set ∩ `{write,remember,delete}_*` empty |
| Handler: flag on + fixture CONTEXT via monkeypatched path / cwd | list text contains seed terms; lookup `"gateway"` → canonical Gateway |
| Handler: flag on + missing CONTEXT | soft empty / not-found text, no raise |

Reuse pattern from `test_engine_mcp_deepseek_web.py` / `test_memory_tools_*`. Skip handler tests when `Server is None` if they require `TextContent`.

---

## 7. Explicit non-goals (T20)

- Ontology write-back / Evo / auto-infer from code.
- Multi-repo federation glossary.
- Auto-inject glossary into every worker prompt.
- Changing CONTEXT seed content (human edits only).
- Live stdio MCP integration test as CI requirement.
- Separate ontology MCP binary (Approach B).

---

## 8. Key file references

- `python/ultimate_coders/agent/ontology.py` (T19 loader)
- `python/ultimate_coders/agent/engine_mcp.py` (registration seam)
- `CONTEXT.md` (human authority)
- `tests/python/test_ontology_loader.py` (loader contracts — leave intact)
- `tests/python/test_engine_mcp_deepseek_web.py` (helper-test peer)
- `tests/python/test_hy_memory_integration.py` (`_memory_tools` peer)
- `docs/architecture/agent-trust-eval/tickets/20-ontology-mcp-readonly.md`
- `docs/architecture/agent-trust-eval/issues/D9-project-ontology-mcp.md`
- `docs/architecture/agent-trust-eval/spec-p3.md`
- `.trellis/spec/backend/ontology-loader-spec.md`
