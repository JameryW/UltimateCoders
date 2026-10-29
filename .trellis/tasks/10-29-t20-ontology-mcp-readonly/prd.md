# T20: ontology MCP read-only tools

## Goal

When `UC_ONTOLOGY_MCP` is on, advertise read-only MCP `list` / `lookup` tools on the UC engine MCP server that wrap T19’s `ontology.list_terms` / `lookup_term`. When the flag is off, hide the tools. Soft-empty on missing CONTEXT; never crash; never ship write/remember/delete ontology tools.

## Source

- Ticket: `docs/architecture/agent-trust-eval/tickets/20-ontology-mcp-readonly.md`
- Decision: [D9](../../docs/architecture/agent-trust-eval/issues/D9-project-ontology-mcp.md)
- Spec: [spec-p3.md](../../docs/architecture/agent-trust-eval/spec-p3.md) Seam I
- Loader: `python/ultimate_coders/agent/ontology.py` + `.trellis/spec/backend/ontology-loader-spec.md` (T19 **implemented**)
- Peer MCP gates: `_memory_tools` / `_deepseek_web_tools` in `engine_mcp.py`

## What I already know

* T19 landed: flag default off, soft-empty loader, root `CONTEXT.md` seed, no MCP registration.
* D9 / P3: human-owned CONTEXT is the only glossary authority; codegraph and Hy-Memory are not; write-back / Evo deferred.
* `engine_mcp.py` already conditionally advertises tools via helpers unit-tested without a full MCP server.
* Ticket acceptance: flag on → list/lookup return loader data; flag off → tools absent or soft-fail; no write APIs; missing CONTEXT soft; tests without a full coding agent; flag defaults off.
* Caller preference: **Approach A** — thin wrappers in `engine_mcp` behind `UC_ONTOLOGY_MCP`; hide when off; soft empty on missing CONTEXT; never write tools.

## Assumptions (temporary)

* Tool names: `list_ontology_terms` / `lookup_ontology_term`.
* All MCP glue lives in `engine_mcp.py`; `ontology.py` stays the authority API (no second parser).
* Plain-text `TextContent` responses (peer to search/memory), including `authority_path`.
* Prefer **hide when off** over register-then-soft-fail (ticket allows either; D9 operator story prefers absent tools).
* Tests primarily call `_ontology_tools()` (+ handlers when `mcp` is installed); no stdio server required in CI.

## Research References

* [`research/grounding.md`](research/grounding.md) — T19 API, engine_mcp peers, acceptance matrix, Approach A tool sketch, test plan

## Research Notes

### Constraints from our repo/project

* Product-facing flags default **off**; same truthy parser as T19 / Hy-Memory.
* Optional `mcp` package: helpers must return `[]` when `Server is None`.
* Loader soft contracts already cover flag-off and missing CONTEXT — MCP layer must not re-raise those cases.
* Spec-p3 Seam I testing: flag off → no tools / soft empty; flag on + missing → soft empty; flag on + seeded → data; no write API.

### Feasible approaches here

**Approach A: thin `_ontology_tools` in `engine_mcp`** (Recommended)

* How: Gate on `ontology_mcp_enabled()` + `Server`; register `list_ontology_terms` / `lookup_ontology_term`; handlers call `list_terms` / `lookup_term`; unit-test the helper.
* Pros: Matches DeepSeek/Hy-Memory peers; reuses T19 authority; smallest ship; ticket-aligned hide-when-off.
* Cons: Ontology advertising stays coupled to the engine MCP process (same as other UC tools).

**Approach B: separate `ontology_mcp.py` stdio server**

* How: New module/entry for ontology-only MCP.
* Pros: Cleaner module boundary.
* Cons: Extra operator config; duplicates boilerplate; diverges from ticket’s engine-MCP expectation.

**Approach C: always register; soft-fail in handlers**

* How: Tools always listed; handlers return empty when flag off.
* Pros: Simpler list_tools.
* Cons: Agents see dead tools by default; weaker “default off” story vs ticket.

## Decision (ADR-lite)

**Context**: T20 must expose T19’s glossary over MCP without a second authority or write path.  
**Decision**: **Approach A** — locked via「继续」.  
**Consequences**: One splice into `engine_mcp`; hide when `UC_ONTOLOGY_MCP` off; soft empty via existing loader; write/Evo remain out of scope.

## Requirements (evolving)

* Add `_ontology_tools()` (name may vary) gated by `ontology_mcp_enabled()` and MCP availability; splice into `list_tools()`.
* Register read-only tools `list_ontology_terms` and `lookup_ontology_term` (or equivalent) when flag on.
* Handlers are thin wrappers over `ontology.list_terms` / `lookup_term`; soft empty / not-found text when CONTEXT missing or term absent.
* When flag off: tools **not** advertised.
* Never provide write / remember / delete / mutate ontology tools or rewrite `CONTEXT.md`.
* Do not treat codegraph or Hy-Memory as glossary sources.
* Unit tests cover the flag matrix via the tool-list helper (and handlers when `mcp` present) without a full coding agent / stdio server.
* Feature flag remains default off (no change to T19 default).

## Acceptance Criteria (evolving)

* [ ] Flag on → `list_ontology_terms` / `lookup_ontology_term` advertised and return loader data from CONTEXT
* [ ] Flag off → ontology tools absent from helper / advertised set
* [ ] Flag on + missing CONTEXT → empty list / not-found, no process crash
* [ ] No ontology write / remember / delete tools
* [ ] Tests cover flag matrix without a full coding agent (helper-based)
* [ ] Feature flag defaults off
* [ ] No second authority format (wrappers only over T19 API)

## Definition of Done (team quality bar)

* Tests added/updated (unit; no live MCP server required)
* Lint / typecheck / CI green for touched Python
* Docs/notes: update ticket status / map when shipping; optional ontology MCP note in loader spec §6
* Rollout: flag stays off by default; safe no-op when unset

## Out of Scope (explicit)

* Ontology write-back / Evo evolution / multi-repo federation
* Separate ontology MCP binary (Approach B)
* Always-register soft-fail default (Approach C) unless reconsidered
* Changing CONTEXT seed vocabulary
* Auto-inject glossary into worker prompts
* Live stdio MCP CI harness as a hard requirement
* True replan / other P3 fog (T18, D10 numeric CI)

## Technical Approach

1. Add `_ontology_tools()` + handlers in `python/ultimate_coders/agent/engine_mcp.py`.
2. Splice into `list_tools` / `call_tool` beside `_memory_tools` / `_deepseek_web_tools`.
3. Add `tests/python/test_engine_mcp_ontology.py` mirroring DeepSeek/Hy-Memory helper tests.
4. Leave `ontology.py` contracts intact unless a tiny export is required.

## Decisions locked

* Approach A: `_ontology_tools()` in `engine_mcp.py`; hide when flag off; soft empty; no write tools.

## Technical Notes

* Peer helpers: `_memory_tools`, `_deepseek_web_tools` in `engine_mcp.py`.
* Loader: `ontology_mcp_enabled`, `list_terms`, `lookup_term`.
* Research: `research/grounding.md`.
* Branch context: `cursor/t20-ontology-mcp-readonly-7c92`.
