# PRD — V8 P4 graph cutover runbook (D6 / D10)

## Background

P0–P3 merged (#869–#872). True replan execute (T18) stays deferred. P4 closes D6’s “ready-for-human” gap: ops runbook + dry-run readiness check, with **no** `UC_GRAPH_SHADOW` default flip.

Authority: [D6](../../../docs/architecture/v8-upgrade/issues/D6-graph-cutover-ownership.md), [D10](../../../docs/architecture/v8-upgrade/issues/D10-graph-cutover-runbook.md), [spec-p4](../../../docs/architecture/v8-upgrade/spec-p4.md). Tickets 13–15.

## Requirements

1. Human-facing cutover/rollback runbook linked from V8 map + `docs/architecture.md`
2. Dry-run readiness script (exact `on` gate; never enables shadow / never mutates Compose)
3. Focused pytest for script helpers
4. Map / agent-os docs for P4

## Out of scope

- Flip Compose or image defaults to `UC_GRAPH_SHADOW=on`
- Graph-only TaskStore removal
- True replan execute / digest bump (T18)

## Done when

spec-p4 AC green; focused pytest pass; census pins refreshed if needed.
