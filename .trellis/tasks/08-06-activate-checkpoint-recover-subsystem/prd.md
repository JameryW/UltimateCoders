# Activate Checkpoint/Recover Subsystem

## Goal

The Rust engine has a complete **event-sourcing checkpoint/recover subsystem**
(`checkpoint.rs`, 641 lines: `CheckpointManager` with `record_event`,
`create_snapshot`, `recover`, `list_events`) that is **fully dormant in
production** — built, unit-tested, but never called outside tests.

Activate it: persist task state snapshots, expose manual checkpoint/recover via
gRPC, and let task resume reconstruct state from snapshot+event-replay instead
of the current pure in-memory status flip.

## What I already know

- `CheckpointManager` (uc-engine/src/checkpoint.rs) is wired into `LocalEngine`
  (`record_event`/`checkpoint_task`/`recover_task` methods), but:
  - `LocalEngine.record_event` is called ONLY in tests (line 1350).
  - `resume_task` (local.rs:931) calls `store.resume_task` — a pure in-memory
    status flip, never touches `recover_task`.
  - `EngineApi` trait (uc-types/src/engine.rs) has NO checkpoint/recover methods.
  - No gRPC RPC exposes checkpoint/recover (engine.proto has PauseTask/ResumeTask
    but nothing for snapshot/restore).
- gRPC `TaskStore` (server.rs:282) has its OWN `event_store: Arc<dyn EventStore>`
  that events ARE persisted to (`record_event_with_subject`, line 472-480).
  This is a parallel path to `CheckpointManager` — both consume `EventStore`.
- `GrpcServerInner` (server.rs:1339) holds `engine: E` + `task_store:
  Arc<Mutex<TaskStore>>`. TaskStore already owns an EventStore.
- `CheckpointManager` requires an `EventStore` (trait, impls: InMemoryEventStore,
  NatsEventStore). It can share TaskStore's existing EventStore.
- Prior dormant-activation pattern (Scheduler, Aug 2026): config PR → engine
  wiring → gRPC routing → spec doc. 4 PRs. Mirror this.
- Snapshot store is in-memory DashMap (`snapshot_store`) — comment at line 116
  says "in production, this would go to TiKV". MVP keeps DashMap.

## Assumptions (temporary)

- MVP: attach CheckpointManager to TaskStore (shares its EventStore), NOT just
  LocalEngine. TaskStore is the production event-recording path.
- Add `checkpoint_task`/`recover_task` to EngineApi trait + proto RPC.
- `resume_task` gains an OPTIONAL recover path (snapshot+replay) — opt-in to
  avoid breaking existing in-memory resume semantics.
- Snapshot store stays in-memory DashMap (no TiKV in MVP).

## Open Questions

- (resolved) Scope: **full activation** — RPC + CheckpointManager on TaskStore
  production path + resume_task reconstructs via recover.

## Requirements (evolving)

- CheckpointManager attached to production event path (TaskStore).
- Manual checkpoint/recover reachable via gRPC.
- resume_task can reconstruct from snapshot (opt-in or default).

## Acceptance Criteria (evolving)

- [ ] CheckpointManager.recover() works against production-recorded events
      (not just test-recorded).
- [ ] New gRPC RPCs: CreateCheckpoint / RecoverTask (or equivalent).
- [ ] resume_task exercises recover path (opt-in flag or default).
- [ ] Unit test: record events via TaskStore → checkpoint → recover → state
      matches.
- [ ] CI green (cargo test -p uc-engine, -p uc-grpc).

## Definition of Done

- Tests added (record→checkpoint→recover round-trip via TaskStore).
- Lint/typecheck/CI green.
- Spec doc updated (cross-layer path, like scheduler-spec).
- Rollout: pure additive (new RPCs + opt-in flag), no breaking change.

## Technical Approach

Attach `CheckpointManager` to `TaskStore` (shares its existing `EventStore`).
Add `TaskStore::checkpoint_task`/`recover_task` methods. Add `checkpoint_task`/
`recover_task` to `EngineApi` trait (with default impls). Add proto RPCs
`CreateCheckpoint`/`RecoverTask`. `resume_task` reconstructs state via recover
before flipping status (default-on — full activation).

## Decision (ADR-lite)

**Context**: 641-line checkpoint subsystem dormant. TaskStore already persists
events to an EventStore; CheckpointManager consumes the same trait but on a
parallel (test-only) path.

**Decision**: Full activation. Wire CheckpointManager into TaskStore (production
path), expose RPCs, make resume_task recover-then-resume. Snapshot store stays
in-memory DashMap (TiKV out of scope).

**Consequences**: resume_task semantics change (now reconstructs from events).
Risk: if event log incomplete, recover returns partial state — mitigate with
fallback to current in-memory resume on recover error. Additive RPCs = no
breaking change for existing callers.

## Implementation Plan (small PRs, mirror scheduler activation)

* PR1: wire CheckpointManager into TaskStore + EngineApi trait methods (config +
  engine wiring + round-trip test)
* PR2: proto RPCs CreateCheckpoint/RecoverTask + gRPC routing + conversions
* PR3: resume_task recover-then-resume path + fallback on error
* PR4: spec doc (cross-layer checkpoint path)

## Out of Scope (explicit)

- TiKV/external snapshot store (stays in-memory DashMap).
- Python worker checkpoint integration.
- OMP UI for checkpoint inspection.
- Snapshot compaction/GC policy.

## Technical Notes

- Files: crates/uc-engine/src/checkpoint.rs, local.rs; crates/uc-grpc/src/
  server.rs; crates/uc-types/src/engine.rs; crates/uc-grpc/proto/engine.proto;
  crates/uc-grpc/src/conversions.rs.
- CheckpointManager.new(event_store, config) — needs shared EventStore.
- Proto pattern: follow PauseTask/ResumeTask request/response shape.
- EngineApi trait methods need default impls (like scheduler methods) so
  existing implementors don't break.
