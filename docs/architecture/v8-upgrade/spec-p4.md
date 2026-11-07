# Spec P4 — Graph-primary cutover runbook (D6 / D10)

Blocked by: D6 + D10 (decided). Does not flip `UC_GRAPH_SHADOW` defaults or implement true replan execute (T18).

## Goal

Make graph-primary durable-runtime cutover **ready for an explicit human/ops authorization**: a checklist runbook plus a dry-run readiness script that never enables shadow mode by itself.

## What to build

1. Runbook under `docs/architecture/` (linked from V8 map + `docs/architecture.md` durable-runtime section) covering: prerequisites, enable shadow (`UC_GRAPH_SHADOW=on` exact token), verify shadow writes / lease-fence health, rollback, and what remains graph-only vs TaskStore projection.
2. Dry-run script `scripts/check-graph-cutover-readiness.py` (or sibling name) that exits non-zero on hard blockers and prints a checklist; must not set env vars that enable shadow; must not rewrite Compose files.
3. Focused tests for the script's pure helpers (gate parsing / checklist assembly) so CI pins the contract.
4. Docs: map P4 + D10; agent-os gap note that runbook landed without default-on.

## Acceptance criteria

- [ ] Runbook states default remains off; enable requires exact `UC_GRAPH_SHADOW=on`
- [ ] Runbook references durable-runtime upgrade notes for lockstep gateway/worker restart
- [ ] Dry-run script never enables shadow / never mutates Compose defaults
- [ ] Script reports at least: whether `UC_GRAPH_SHADOW` is currently on, whether `UC_DATABASE_URL` is set, and pointers to the runbook
- [ ] Tests cover gate parsing alignment with gateway (`on` exact) and dry-run non-mutation
- [ ] Map / agent-os mark P4 runbook landed; true replan execute still deferred

## Out of scope

- Flipping Compose or image defaults to `UC_GRAPH_SHADOW=on`
- Graph-only TaskStore removal
- True replan execute / digest bump (T18)
- Changing lease/fence/commit-once product semantics
