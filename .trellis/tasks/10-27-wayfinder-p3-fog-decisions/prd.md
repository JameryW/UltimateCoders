# Wayfinder P3 fog → D9/D10 decisions

## Goal

Graduate the two P2-destination fog items (project ontology MCP; README numeric CI) into decided grilling tickets D9/D10 so the next turn can `$to-spec` / `$to-tickets` for P3.

## Source

- Map fog (pre-decision): ontology MCP + README numeric CI
- Research: `research/grounding.md`
- User「继续」adopts Proposed for both (same pattern as D7/D8)

## Decisions locked

* **D9:** human-owned `CONTEXT.md` + read-only MCP; flag default off; no write-back / Evo in MVP
* **D10:** numeric manifest + pin checker hard fail (SSE family first); live remeasure deferred; align D1/T2 on uplift %

## Out of scope

Implementing MCP tools or CI scripts in this change; T18 true replan; dataset-split / label-pipeline fog.

## Acceptance Criteria

* [x] `issues/D9-project-ontology-mcp.md` decided
* [x] `issues/D10-readme-numeric-ci.md` decided
* [x] `map.md` updated (D9–D10; fog list trimmed; frontier = P3 not ticketed)
* [ ] Docs PR opened
