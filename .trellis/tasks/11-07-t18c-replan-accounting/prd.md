# PRD — T18c durable replan accounting (D13)

## Background

T18a proposal + T18b checklist merged (#872/#875). `replans_used` is still caller-supplied per stall — gateway cannot enforce `max_replans` durably. Full digest-bump execute stays deferred.

## Requirements

1. `WorkCase.replans_used` persisted (default 0)
2. `UC_REPLAN_ACCOUNTING_ENFORCE` default off — when on, store drives staircase; replan verdict increments
3. Docs + tests; mark T18b merged #875 on map

## Out of scope

Digest bump, re-decompose, admit_execution, TaskStore/graph mutation.

## Done when

spec-t18c AC green; focused tests pass.
