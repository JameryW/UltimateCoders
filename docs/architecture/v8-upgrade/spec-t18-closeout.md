# Spec — T18 precursor closeout (docs)

Blocked by: D15 (decided). No product behavior change.

## Goal

Close the T18a–d precursor ladder as **complete** after #872 / #875 / #876 / #877, and park true replan execute as a deferred follow-on on trust-eval ticket 18.

## What to build

1. Update `map.md`: mark T18d **merged** (#877); D15 listed; Follow-on precursors complete; Frontier empty with execute deferred pointer.
2. Brief ADR / assessment note that T18a–d precursors landed; execute still deferred.
3. agent-os §5: one-line that T18 precursor wave closed; ticket 18 still owns execute.
4. Note on trust-eval ticket 18 that the precursor ladder closed without satisfying execute.
5. Docs-only ticket; mark implemented when map/ADR/assessment agree.

## Acceptance criteria

- [x] Map marks T18d **merged** (#877); D15 linked; precursor follow-ons complete
- [x] Frontier does not list grabable implement tickets
- [x] T18 execute / Learning OS / graph default-on remain deferred or human-gated
- [x] No Compose/`UC_*` default flips; no replan execute code

## Out of scope

- True replan execute / digest bump / `admit_execution`
- Flipping `UC_GRAPH_SHADOW` defaults
- Learning OS
- New precursor implementation slices
