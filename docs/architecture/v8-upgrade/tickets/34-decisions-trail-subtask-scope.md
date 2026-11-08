# 34 — Decision trail subtask scope (T34)

**What to build:** `subtask_id` in `decisionTrailQuery`, `decisionScopeLabel`, scope
buttons on the list, and scope-aware state/paging in `DecisionRecordTrail` — per
[spec-p11.md](../spec-p11.md). Dashboard only: the endpoint already accepts
`subtask_id`.

**Blocked by:** D21

**Status:** implemented (`dashboard/src/lib/decisions.ts`,
`dashboard/src/components/panels/DecisionRecordPanel.tsx`,
`dashboard/tests/decisions.test.mjs`)

- [x] Query adds `subtask_id` only when scoped
- [x] Row subtask ids are scope buttons while task-wide; "all subtasks" resets
- [x] Paging inside a scope keeps the scope and the cursor
- [x] Scoped empty state names the subtask
