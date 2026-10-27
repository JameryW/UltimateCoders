# Agent Trust / Eval — P3 Spec

Status: **ready-for-agent** (planning artifact; no product code in this change)
Authority: [D9](issues/D9-project-ontology-mcp.md), [D10](issues/D10-readme-numeric-ci.md); inherits P0 D1/T2 report-only uplift and P2 flag culture
Map: [map.md](map.md)
Depends on: P0–P2 tickets 01–17 implemented; D9–D10 decided

## Problem Statement

P0–P2 made eval identity, dual correctness, completion, approx prune, typed review, Hy-Memory, stall redispatch, and child NetworkMode/Landlock enforceable behind flags. Two destination items that stayed fog through P2 are now decided: (1) there is still no project ontology agents can query without inventing a third “truth” beside codegraph and Hy-Memory; (2) README/docs numbers can drift without a hard CI pin, and uplift percentages could re-enter docs without a report-role provenance check.

## Solution

Ship P3 as two coordinated seams, flags default off where product-facing:

1. **Project ontology MCP (D9).** Human-owned root CONTEXT authority file + read-only MCP `list` / `lookup` behind `UC_ONTOLOGY_MCP` (default off). Soft empty when file absent or flag off. No Agent write-back / Evo evolution in MVP. Codegraph and Hy-Memory remain non-authorities for glossary terms.

2. **Docs numeric pin CI (D10).** Machine-readable numeric manifest + hard-fail pin checker (SSE latency family first). Ban undocumented uplift `%` on README-facing surfaces unless traceable to `eval_role=report`. Live stack remeasure and GPU/BenchmarkRunner recompute stay deferred.

## User Stories

1. As a coding agent, I want read-only ontology lookup when the flag is on, so that I use project terms consistently.
2. As a domain owner, I want CONTEXT to be human-edited only, so that agents cannot silently rewrite authority.
3. As an operator with the flag off, I want no ontology tools registered (or soft-fail), so that defaults stay compatible.
4. As a docs maintainer, I want CI to hard-fail when README SSE numbers drift from the manifest pin.
5. As a trust reviewer, I want undocumented uplift percentages rejected from README-facing docs.
6. As an implementer of ticket 20, I want ticket 19’s loader/flag, so that MCP tools do not invent a second authority format.
7. As a wayfinder reader, I want P3 tickets linked from the map as ready-for-agent after D9–D10.
8. As a corpus guardian, I want planning docs to reuse existing CONTEXT exemption patterns rather than inventing new untracked paths casually.

## Implementation Decisions

- **CONTEXT is authority.** Loader reads root CONTEXT (or fail soft / empty); never codegraph/Hy-Memory as glossary source.
- **Read-only MCP.** `list` / `lookup` only; no remember/write tools for ontology in P3.
- **Flag default off.** `UC_ONTOLOGY_MCP` mirrors other UC truthy parsers.
- **Numeric manifest hard fail.** Pin checker runs in CI without a live cluster; SSE family is the first pin set.
- **Uplift provenance.** Any README uplift `%` must be on the manifest with report-role provenance or be forbidden.
- **Live remeasure deferred.** Separate later ticket; does not block pin checker.

## Testing Decisions

1. **Seam I — ontology.** Flag off → no tools / soft empty. Flag on + missing CONTEXT → soft empty. Flag on + seeded CONTEXT → list/lookup return terms. No write API.
2. **Seam J — numeric pins.** Manifest/doc mismatch → hard fail. Undocumented uplift `%` in README surface → fail. Live remeasure not required for MVP green.

## Out of Scope

- True replan (ticket 18 deferred).
- Ontology write-back / Evo evolution / multi-repo federation.
- Live SSE remeasure in PR CI; GPU BenchmarkRunner doc recompute.
- Publishing GitHub wayfinder issues without authorization.
- Dataset split / label-pipeline fog (still not decision-ready).

## Further Notes

- Feature flags for P3 product surfaces stay **off by default**.
- Vertical slices: T19 none → T20←T19; T21 none → T22 deferred live remeasure. Frontier after publish: **19, 21**.
