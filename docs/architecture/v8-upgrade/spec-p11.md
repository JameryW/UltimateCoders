# Spec P11 — Decision trail subtask scope

Blocked by: D21 (decided). Dashboard only; the endpoint already accepts `subtask_id`.

## Goal

Let an operator narrow the Decision Record trail to one subtask and page inside that
scope, mirroring the context-compile trail on the same panel.

## What to build

1. **Lib**: `decisionTrailQuery(taskId, {subtaskId, limit, order, before})` adds
   `subtask_id` when scoped; `decisionScopeLabel(subtaskId?)` returns `""` or
   `subtask <id> · `.
2. **List**: `DecisionRecordList` gains an optional `onSelectSubtask`; the row's
   subtask id becomes a button when supplied, a plain label otherwise.
3. **Trail**: scope state keyed by task (`{taskId, subtaskId}`); the loaded trail is
   valid only for its identity; first page and "load older" both carry the scope;
   "all subtasks" resets; the scoped empty state names the subtask.
4. **Tests**: query includes/omits `subtask_id`; scope label; SSR renders the scope
   button only when a callback is supplied and the scoped empty sentence.

## Acceptance criteria

- [ ] A task-wide trail renders subtask buttons; a scoped trail does not.
- [ ] "Load older" inside a scope keeps `subtask_id` and the cursor.
- [ ] Switching task or scope never shows the previous window.
- [ ] The scoped empty state names the subtask.
- [ ] No new endpoint/parameter; `tsc` (app + node) and `vite build` pass.

## Out of scope

- Verdict/route filtering
- Persisted scope across task switches
- Any backend change
