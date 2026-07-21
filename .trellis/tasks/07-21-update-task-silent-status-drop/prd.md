# update-task-silent-status-drop

## Goal

`TaskStore::update_task` (`crates/uc-grpc/src/server.rs:703-706`) updates a
task's status with:
```rust
if let Ok(parsed) = proto_status_to_task_status(status) {
    task.status = parsed;
}
task.updated_at = chrono::Utc::now();
```
On an unrecognized status string, the `Err` is silently dropped — the task
keeps its OLD status, but `updated_at` is still refreshed. Callers see a
status that never changed yet a new timestamp, silently masking the rejected
update. `update_task` returns `Result<_, String>` so it can surface the
error instead.

## What I already know

- Bug site: `server.rs:702-706` in `TaskStore::update_task`.
- `proto_status_to_task_status` (`conversions.rs:816`) returns
  `Err("Unknown TaskStatus: {s}")` for unrecognized strings — clear message.
- `update_task` signature returns `Result<(Task, Vec<AgentEventType>), String>`
  — can propagate Err.
- Caller: gRPC `update_task` RPC handler (server.rs:3482) maps Err → status.
- Edge: the create-if-not-exists branch (server.rs:680) uses
  `unwrap_or(TaskStatus::Created)` — that's an intentional default for a new
  task, leave as-is. Only the existing-task update path (703) is the bug.

## Requirements

- On unrecognized status: return `Err(proto_status_to_task_status error)` —
  do NOT silently keep old status + refresh updated_at.
- Refresh `updated_at` only when status actually changes (or keep refreshing
  on the success path — but the failure path must not refresh).
- Minimal: convert `if let Ok` to a `?`-style propagation.

## Acceptance Criteria

- [ ] Unrecognized status string → `update_task` returns Err naming the bad status.
- [ ] `updated_at` NOT refreshed on the error path (task untouched).
- [ ] Valid status → unchanged behavior (status updates, updated_at refreshes).
- [ ] `cargo test -p uc-grpc` green, incl regression test.

## Definition of Done

- Fix + regression test.
- `cargo check/fmt/clippy` clean.
- PR opened + CI green + merged.

## Out of Scope

- create-if-not-exists default (intentional).
- Other scan findings.

## Technical Notes

- `crates/uc-grpc/src/server.rs:663-706` (`update_task`, status update block).
- `crates/uc-grpc/src/conversions.rs:816` (`proto_status_to_task_status`).
