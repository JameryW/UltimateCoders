# T19: CONTEXT ontology loader + flag

## Goal

Seed a minimal human-owned root `CONTEXT.md` and an in-process loader (`ontology.py`) gated by `UC_ONTOLOGY_MCP` (default off), so T20 can expose read-only MCP `list`/`lookup` without inventing a second authority format. Soft-empty when the flag is off or the file is missing; no MCP registration and no write APIs in this ticket.

## Source

- Ticket: `docs/architecture/agent-trust-eval/tickets/19-context-ontology-loader.md`
- Decision: [D9](../../docs/architecture/agent-trust-eval/issues/D9-project-ontology-mcp.md)
- Spec: [spec-p3.md](../../docs/architecture/agent-trust-eval/spec-p3.md) Seam I
- Domain pointer: `docs/agents/domain.md` → lazy root `CONTEXT.md`
- Format: `.agents/skills/domain-modeling/CONTEXT-FORMAT.md`

## What I already know

* Root `CONTEXT.md` is **absent**; domain.md already names it as canonical vocabulary when present.
* D9 / P3: human-owned CONTEXT is the only glossary authority; codegraph and Hy-Memory are not.
* Peer pattern: `hy_memory.py` `UC_HY_MEMORY_AUTO_CONTEXT` + truthy parser + soft skip (`[]`/`None`, never raise).
* `engine_mcp.py` conditionally advertises tools; T20 will register ontology tools there (or sibling) — **not** T19.
* Ticket acceptance: flag defaults off; loader soft-empty on flag-off / missing file; parse when seeded; no write/delete APIs; tests without a live MCP server.

## Assumptions (temporary)

* Module path: `python/ultimate_coders/agent/ontology.py` (peer to `hy_memory.py`).
* Seed vocabulary stays small (~7 orchestration terms); trust/eval nouns deferred to later human edits.
* Lookup is case-insensitive; canonical casing comes from the file.
* Optional `authority_path` defaults to `CONTEXT.md`; per-term overrides not required for MVP.

## Research References

* [`research/grounding.md`](research/grounding.md) — domain.md / CONTEXT-FORMAT, MCP+Hy-Memory peers, seed terms, Approach A API sketch

## Research Notes

### Constraints from our repo/project

* Single-context repo; Matt `**Term**:` / `_Avoid_:` Markdown at repo root.
* Feature flags product-facing → **off by default**; same `_TRUTHY_ENV` set as Hy-Memory / Landlock.
* Spec-ref guards already exempt dangling CONTEXT mentions until this ticket creates the file.

### Feasible approaches here

**Approach A: CONTEXT.md + `ontology.py` loader** (Recommended)

* How: Seed minimal root `CONTEXT.md`; implement flag + `OntologyTerm` + `load_ontology` / `list_terms` / `lookup_term`; soft empty when flag off or file missing; no MCP registration; no write APIs.
* Pros: Matches domain.md + D9 + ticket 19; mirrors Hy-Memory flag culture; gives T20 a stable contract.
* Cons: Must create currently-absent CONTEXT; Markdown parser must tolerate light drift.

**Approach B: docs-path glossary + loader**

* How: Same API but authority at `docs/agents/glossary.md` (or similar).
* Pros: Cleaner repo root.
* Cons: Diverges from Matt default and domain.md pointer.

**Approach C: Generate from code enums / protos**

* How: Build term store from `FailureKind`, protos, etc.
* Pros: Always “in sync” with typed enums.
* Cons: Wrong layer; contradicts D9 human authority; misses architectural nouns.

## Decision (ADR-lite)

**Context**: T19 needs a loader authority and API shape before T20 registers MCP tools.  
**Decision**: **Approach A** (recommended) — pending confirmation.  
**Consequences**: Humans own `CONTEXT.md`; loader is read-only and flag-gated; T20 wraps the same structures; write-back / Evo remain deferred.

## Requirements (evolving)

* Add `UC_ONTOLOGY_MCP` (`ONTOLOGY_MCP_ENV`) with `ontology_mcp_enabled()`; default **off**.
* Implement `OntologyTerm` + `load_ontology` / `list_terms` / `lookup_term` in `python/ultimate_coders/agent/ontology.py`.
* Soft empty: flag off or missing/unreadable CONTEXT → `[]` / `None`; do not raise for those cases.
* Seed minimal root `CONTEXT.md` (Matt Language format) with few UC orchestration terms (see grounding §3 draft).
* Do **not** register MCP tools (T20). Do **not** add write/delete/remember APIs.
* Do **not** read glossary content from codegraph or Hy-Memory.

## Acceptance Criteria (evolving)

* [ ] `UC_ONTOLOGY_MCP` exists and defaults off
* [ ] Loader returns empty / soft-skip when flag off or CONTEXT absent
* [ ] Loader returns parsed terms when CONTEXT is present (fixture and/or seeded file)
* [ ] `list_terms` / `lookup_term` behave consistently with `load_ontology`
* [ ] Codegraph / Hy-Memory are not used as glossary sources
* [ ] No ontology write / delete APIs
* [ ] Tests cover flag-off, missing file, and seeded parse without a live MCP server
* [ ] Feature flag defaults off

## Definition of Done (team quality bar)

* Tests added/updated (unit; no live MCP server required)
* Lint / typecheck / CI green for touched Python
* Docs/notes updated if behavior changes (ticket/spec cross-links already exist; product doc only if needed)
* Rollout: flag stays off by default; safe no-op when unset

## Out of Scope (explicit)

* MCP tool registration / advertising in `engine_mcp` (T20)
* Ontology write-back / Evo evolution / multi-repo federation
* Treating codegraph or Hy-Memory as glossary authority
* Auto-inject of glossary into worker prompts
* Full trust-eval / NetworkMode glossary expansion beyond the minimal seed
* `CONTEXT-MAP.md` / multi-context layout

## Technical Approach

1. Seed root `CONTEXT.md` with ~7 Matt-format Language terms (Gateway, Worker, Task, Subtask, Attempt, ExecutionEnvelope, CONTRACT_VERSION).
2. Add `ontology.py`: truthy flag helper, dataclass, Markdown parse → terms, list/lookup wrappers, soft empty.
3. Unit tests under `tests/python/test_ontology.py` (tmp_path fixtures; env matrix).
4. Leave `engine_mcp.py` unchanged for T19.

## Open Questions

* Confirm Approach A (vs B/C) and the ~7-term seed list — or trim/expand before implement.

## Technical Notes

* Peer flag/soft-skip: `python/ultimate_coders/agent/hy_memory.py` (`HY_MEMORY_AUTO_CONTEXT_ENV`, `_TRUTHY_ENV`).
* T20 consumer: `docs/architecture/agent-trust-eval/tickets/20-ontology-mcp-readonly.md`.
* Entity vocabulary source for seed drafts: `docs/agent-os-architecture.md` §3 + AGENTS.md contract handshake.
* When CONTEXT.md is created, dangling-ref exemptions in `scripts/check-spec-refs.py` may need a follow-up count/exemption revisit (implementation detail).
