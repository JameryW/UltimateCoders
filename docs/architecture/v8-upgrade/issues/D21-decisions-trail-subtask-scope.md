# D21 — Subtask scope for the Decision Record trail

Status: decided
Decided: 2026-11-09

## Question

D19 deferred a subtask-scope selector with a specific reason: "adding a control without
a cursor would just narrow a bounded window". D20 then added the cursor. Should the
Decision Record trail gain scope now, and how should scope and paging interact?

## Decision

**Yes: the trail scopes by subtask and pages inside that scope. No new endpoint or
query parameter — `subtask_id` already exists and `order=recent` + `before` already
accept it.**

1. **Control.** Each row's subtask id becomes a scope button while the trail is
   task-wide; a scoped trail shows an "all subtasks" reset, exactly like the
   context-compile trail next to it.
2. **Paging stays inside the scope.** The scoped request carries `subtask_id` with
   `order=recent` and `before`, so "load older" continues that subtask's trail rather
   than jumping back to the task-wide window.
3. **Scope is part of the loaded state's identity.** A task or scope switch never
   shows the previous window while the new one is in flight.
4. **Empty states name the scope**: "No attempts recorded for this subtask." instead
   of the task-wide sentence.
5. **No verdict/route filtering.** That would be a different mechanism with different
   server-side semantics; it is not part of this slice.

## Why

The objection D19 recorded was about a control with nothing to page — D20 removed it.
Two adjacent audit trails on the same panel answering "narrow it to one subtask" in
two different ways is the drift this repository keeps paying for; the manifest trail
already has this control.

## Boundary

- No new route, parameter, or outbox method.
- No persisted scope across task switches.
- No filtering by verdict or route.

## Implementation ticket

T34: scope in the trail query + list scope buttons + panel scope state/paging + tests.
