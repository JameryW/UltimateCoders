# events-synthetic-taskcreated-on-parse-fail

## Goal

`JetStreamEventStore::read_from` (`crates/uc-engine/src/events.rs:371-375`)
parses each replayed NATS message with:
```rust
serde_json::from_slice(&message.payload)
    .unwrap_or_else(|_| AgentEventType::TaskCreated {
        task_id: TaskId::new(),  // RANDOM id
        description: String::new(),
    })
```
On parse failure it fabricates a **synthetic `TaskCreated` with a random
TaskId** and pushes it to the replayed results. This is consumed by
checkpoint recovery (`checkpoint.rs:154/177/220` → `apply_event_to_snapshot`
at checkpoint.rs:356), which sets `snapshot.status = "created"`, clobbering
real recovered state whenever a malformed/legacy event sits in the
`AGENT_EVENTS` JetStream subject.

Random TaskId also pollutes the snapshot's task id mapping.

## What I already know

- Bug site: `events.rs:370-385`, the `Ok(Some(Ok(message)))` arm.
- Consumers: `checkpoint.rs:154,177,211,220` (read_from), which call
  `apply_event_to_snapshot`.
- `apply_event_to_snapshot` (`checkpoint.rs:354-358`): `TaskCreated{..}` →
  `snapshot.status = "created"`.
- This is the JetStream (storage-feature) `EventStore` impl; InMemory impl
  (events.rs:249) stores typed events, no parse step, no bug.
- Trigger: a malformed/legacy event in `AGENT_EVENTS` (schema drift, partial
  write, old version payload). Recovery path only, but data-corrupting when
  it hits.

## Requirements

- On parse failure: log a warning with the sequence number + error, then
  **skip** the message (continue the loop) — do NOT fabricate an event.
- Skipping is the right call for a recovery/replay path: one bad message
  shouldn't abort the whole replay; best-effort recovery.
- Keep pushing the sequence offset so the loop advances.

## Acceptance Criteria

- [ ] A malformed message in the stream is skipped (warned), not synthesized.
- [ ] Good messages before/after the bad one still replay.
- [ ] No random TaskId leaks into snapshot state.
- [ ] `cargo test -p uc-engine` green, incl a regression test feeding a bad
      payload through read_from (or the parse path) and asserting skip.

## Definition of Done

- Fix + regression test.
- `cargo check/fmt/clippy` clean (storage feature incl where reachable).
- PR opened + CI green + merged.

## Out of Scope

- Changing EventStore trait signature.
- `server.rs:703` status drop (separate).
- `dispatcher.rs:193` NATS publish drop (separate).

## Technical Notes

- `crates/uc-engine/src/events.rs:336-392` (`JetStreamEventStore::read_from`).
- `crates/uc-engine/src/checkpoint.rs:354-358` (`apply_event_to_snapshot` TaskCreated arm).
- Feature: `storage` (JetStream impl is storage-gated).
