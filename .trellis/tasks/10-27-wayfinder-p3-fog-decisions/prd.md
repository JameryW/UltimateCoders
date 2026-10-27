# wayfinder-p3-fog-decisions

## Goal

Graduate the two remaining Agent Trust wayfinder fog items into decision tickets **D9** (project ontology via MCP) and **D10** (README/docs numeric recomputation CI), with Proposed resolutions matching D7/D8 style: MVP small, flags/guards conservative, hard parts deferred.

## What I already know

* P0–P2 MVP shipped (tickets 01–17); T18 true replan deferred; ontology MCP + README numeric CI remain fog (`map.md`).
* D7/D8 pattern: MVP slice + defer hard half + clear ownership; product flags default off.
* No `CONTEXT.md` / `docs/adr/` yet; `docs/agents/domain.md` already names CONTEXT as glossary authority.
* MCP exists (engine search/memory/Hy-Memory; codegraph symbols) but no domain-term tools.
* README has no uplift `%`; primary measured perf claims are SSE localhost maxima 6.394ms / 55.712ms (EN+ZH + verification doc).
* `check-readme-ci-table.py` is the structural docs↔YAML hard-fail pattern; `verify-sse-latency.py` can remeasure SSE.

## Research References

* [`research/grounding.md`](research/grounding.md) — full grounding + Proposed D9/D10

## Proposed decisions (pending adopt)

### D9 — Project ontology via MCP (Recommended Approach A)

**MVP = read-only glossary MCP over `CONTEXT.md`; evolution deferred.**

1. Minimal human-owned `CONTEXT.md` vocab (runtime entities + trust/eval nouns).
2. Read-only MCP list/lookup behind default-off flag; fail soft when off/missing.
3. Not codegraph / Hy-Memory / layered memory as authority.
4. Defer write-back, auto-evolution, multi-context maps.

### D10 — README / docs numeric CI (Recommended Approach A)

**MVP = pin known measured claims to evidence (hard fail); live remeasure deferred.**

1. Seed with SSE latency claim family; manifest + checker like `check-readme-ci-table`.
2. Uplift `%` banned unless report-role + manifest (align D1/T2); no GPU recompute here.
3. Hard fail pin drift; no required live SSE on every PR.
4. Defer auto-README rewrite, production SLAs, BenchmarkRunner CI recompute.

## Requirements (evolving)

* Record D9/D10 issue files with Status + Resolution once Proposed is adopted (same as D4–D8).
* Update map «Not yet specified» / Decisions so far + assessment fog rows.
* Keep implementation of MCP tools / CI scripts as **follow-on tickets** after decide (this task may stop at decision docs if grilling adopts Proposed).

## Acceptance Criteria (evolving)

* [ ] `research/grounding.md` captures evidence + Proposed D9/D10
* [ ] User/grilling adopts or amends Proposed
* [ ] `issues/D9-*.md` + `issues/D10-*.md` show decided Resolution
* [ ] `map.md` + assessment reflect D9/D10; fog rows cleared or narrowed
* [ ] Local path-mention / line-ending pins updated if guards fail

## Definition of Done

* Decision docs + map/assessment consistent
* Lint/CI hygiene for touched docs
* Docs-only commit on the P3 decisions branch; no product code required in the decide slice

## Out of Scope

* Implementing ontology MCP server or numeric CI script (post-decision tickets)
* T18 true replan; D5 labeling pipeline fog; GitHub wayfinder publication
* Production GPU uplift claims

## Open Questions

* Prefer adopt Proposed D9/D10 as written, or amend authority path (CONTEXT.md vs docs/agents/glossary.md) / CI slice?

## Technical Notes

* Style refs: `docs/architecture/agent-trust-eval/issues/D7-*.md`, `D8-*.md`
* Grounding file is the detailed evidence log; keep PRD thin
