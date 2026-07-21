# dispatcher-nats-publish-silent-drop

## Goal

`OrchestratorDispatcher::_process_decomposition_reply`
(`crates/uc-engine/src/scheduler/dispatcher.rs:208`) publishes each subtask to
`uc.subtask.execute` with:
```rust
let _ = tokio::runtime::Handle::current()
    .block_on(async { c.publish(subject, payload.into()).await });
```
The publish error is silently dropped. A failed publish leaves the subtask
Assigned in the registry but never delivered to `uc.subtask.execute`, so it
stalls until the stale-assigned reaper reverts it — invisible to the operator.

## What I already know

- Bug site: `dispatcher.rs:204-210` (per-subtask publish loop).
- `_process_decomposition_reply` returns `Result<(), EngineError>` — can
  propagate. Caller `dispatch()` (dispatcher.rs:98) already logs + returns
  Err → task marked failed.
- Path active: `messaging` default feature; called on every schedule-trigger
  reply.
- `TaskId` has no `Display`, only `Debug` — use `{:?}`.

## Requirements

- On publish failure: return `EngineError::ConnectionError` naming the
  subtask, so dispatch() logs it + the task surfaces as failed rather than
  stalling.
- No silent `let _ =`.

## Acceptance Criteria

- [ ] Publish error propagated as Err (no `let _ =`).
- [ ] `cargo check/clippy/fmt -p uc-engine` clean.
- [ ] Existing engine tests green.

## Definition of Done

- Fix applied.
- PR opened + CI green + merged.

## Out of Scope

- Per-layer partial-publish recovery (returning on first failure is the
  simplest correct behavior — remaining layers unsent, task fails).
- Unit test (publish path needs async_nats client mock; fix is straightforward
  error propagation verified by type-check + existing suite).
- Other scan findings.

## Technical Notes

- `crates/uc-engine/src/scheduler/dispatcher.rs:204-214` (publish loop + return).
- Caller: `dispatcher.rs:92-100` (dispatch Err handling).
