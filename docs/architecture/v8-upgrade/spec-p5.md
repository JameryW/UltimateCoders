# Spec P5 — V8 MVP closeout (docs)

Blocked by: D11 (decided). No product behavior change.

## Goal

Close the V8 upgrade wayfinder as **MVP complete** after P0–P4 merges, and park true replan execute as a deferred follow-on on trust-eval ticket 18.

## What to build

1. Update `map.md`: status complete; P4 marked **merged** (#873); D11 listed; Frontier empty with T18 deferred pointer.
2. Brief ADR / assessment note that checklist P0–P4 landed.
3. agent-os §5: optional one-line that V8 MVP wave closed (P0–P4); T18 still deferred.
4. Ticket 16 docs-only; mark implemented when map/ADR/assessment agree.

## Acceptance criteria

- [x] Map status reads complete (MVP); P0–P4 cite #869–#873
- [x] D11 decided and linked
- [x] Frontier does not list grabable implement tickets
- [x] T18 / Learning OS / graph default-on remain deferred or human-gated as before
- [x] No Compose/`UC_*` default flips; no replan execute code

## Out of scope

- True replan execute / digest bump
- Flipping `UC_GRAPH_SHADOW` defaults
- Learning OS
