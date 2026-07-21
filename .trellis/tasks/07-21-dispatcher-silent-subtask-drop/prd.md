# dispatcher-silent-subtask-drop

## Goal

`OrchestratorDispatcher::_process_decomposition_reply`
(`crates/uc-engine/src/scheduler/dispatcher.rs:150-153`) parses the
decomposition reply's subtasks with `filter_map(|v| from_value(v.clone()).ok())`,
**silently dropping** any subtask that fails to deserialize. Combined with
`dependency.rs:40` ("Unknown dependency → ignore"), a surviving subtask whose
`depends_on` references a dropped subtask's ID has that dependency silently
ignored → runs **before** its dependency, producing wrong execution order /
data corruption.

This path is ACTIVE: `messaging` is a default feature, `dispatch()` (line 92)
calls `_process_decomposition_reply` on every schedule-trigger reply.

## What I already know

- `_process_decomposition_reply` (dispatcher.rs:131) — underscore-prefix is
  misleading; it IS called (dispatcher.rs:92).
- Current parse: `subtasks.iter().filter_map(|v| serde_json::from_value(v.clone()).ok())`.
- Only warns when `parsed.is_empty()` (ALL failed) → partial drops invisible.
- `dependency.rs:40` silently ignores depends_on IDs not in the parsed set.
- Reply source: Python Orchestrator decomposes via LLM → JSON; a malformed
  subtask (bad field type, missing required field) triggers the drop.

## Requirements

- If ANY subtask fails to parse, do NOT partially execute. Return
  `EngineError::TaskError` naming the failed subtask index + parse error, so
  the task surfaces as failed rather than running with a corrupted dependency
  graph.
- Keep the all-fail warn path but route it through the same Err (or keep
  distinct — either is fine as long as no silent partial dispatch).
- Ponytail: simplest correct fix — collect parse failures, return Err if any.

## Acceptance Criteria

- [ ] Decomposition reply where 1 of N subtasks fails to parse → dispatch returns Err (task marked failed), NOT partial dispatch.
- [ ] All-parse-ok → unchanged behavior.
- [ ] All-fail → Err (was Ok with warn).
- [ ] `cargo test -p uc-engine` green, incl new regression test for partial-drop.

## Definition of Done

- Fix + regression test.
- `cargo check/fmt/clippy` clean.
- PR opened + CI green + merged.

## Out of Scope

- `dependency.rs:40` ignore-unknown semantics (intentional for cross-task deps;
  fixing the dispatcher to never drop makes the ignore safe for in-task).
- `events.rs:371` synthetic-TaskCreated (separate task).
- Other scan findings.

## Technical Notes

- `crates/uc-engine/src/scheduler/dispatcher.rs:131-158` (`_process_decomposition_reply`).
- `crates/uc-engine/src/scheduler/dependency.rs:34-42` (ignore-unknown).
- Feature: `messaging` (default).
