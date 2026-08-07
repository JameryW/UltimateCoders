# Activate task_backend Postgres Persistence

## Goal

`TaskStore.task_backend: Option<Arc<dyn TaskStoreBackend>>` is stored but
**never read** (`#[allow(dead_code)]`, server.rs:291 "stored for future wiring").
`PostgresTaskBackend` (full CRUD via sqlx) is constructed in `uc-grpc-server/main.rs`
when `UC_TASK_BACKEND=postgres`, passed to TaskStore, and silently ignored —
TaskStore's sync methods use the in-memory HashMap exclusively.

User impact: setting `UC_TASK_BACKEND=postgres` logs "TaskStore using PostgreSQL
backend" but tasks are NOT persisted. Restart loses all tasks. Silent
expectation violation.

Activate write-path persistence: fire-and-forget backend upsert on every task
mutation, mirroring the existing `record_event_with_subject` spawn pattern for
EventStore. HashMap stays the read source of truth (restart recovery from PG
is a separate, larger concern — out of scope).

## What I already know

- `task_backend` field: server.rs:292, `#[allow(dead_code)]`, set in 3
  constructors (`with_backend`, `with_backends`, `with_nats_timeout_and_backends`),
  `None` in `new`/`with_event_store`.
- `PostgresTaskBackend` impl: task_store.rs:295 (INSERT/SELECT/UPDATE/DELETE,
  has `pool: Option<...>` + `fallback: InMemoryTaskBackend`).
- Constructed in main.rs:53 when `UC_TASK_BACKEND=postgres`.
- TaskStore methods all sync (`fn`), use `HashMap`. Backend methods async.
- `record_event_with_subject` (server.rs:472) already does fire-and-forget
  `tokio::spawn` for EventStore.append — mirror this for backend writes.
- Mutation points (where `self.tasks` changes): submit_task (543),
  submit_task_pending (580), apply_update (subtask status), pause/resume/cancel,
  update_task.

## Assumptions (temporary)

- MVP: write-path only. Fire-and-forget `update_task` (upsert) on each mutation.
- Reads stay in-memory (HashMap). PG is write-ahead for future restart recovery.
- No TaskStore method signature changes (keep sync; spawn the async backend call).

## Open Questions

- (resolved) Scope: **read+write PG**, but **staged** — write-path first,
  then fix backend semantic gaps, then read-path. HashMap becomes cache.

## Decision (ADR-lite)

**Context**: task_backend stored but never read. User sets
UC_TASK_BACKEND=postgres expecting persistence; tasks stay in-memory (silent
data loss on restart). Backend methods have semantic gaps vs TaskStore's
sync contract (update_task is UPDATE not upsert; pause/resume don't return Task).

**Decision**: Full read+write PG activation, staged across 3 PRs to keep each
reviewable + revertable:
- PR1: write-path fire-and-forget upsert on every mutation (HashMap stays
  read source; PG is write-ahead). No signature changes.
- PR2: fix backend semantic gaps (update_task → upsert via INSERT ON CONFLICT;
  pause/resume/cancel return full Task via re-SELECT).
- PR3: read-path — TaskStore reads delegate to backend when present (HashMap
  becomes cache/fallback). Methods turn async; call sites + inline tests updated.

**Consequences**: PR3 is the risky one (async signature change touches ~15 call
sites + ~20 inline tests). PR1+PR2 are additive + safe. If PR3 stalls, PG is
still write-ahead (durable writes, in-memory reads) — strictly better than today.

## Implementation Plan (staged PRs)

* PR1: write-path persistence — persist_task spawn helper, called at every
  mutation point; remove #[allow(dead_code)].
* PR2: backend semantic fixes — upsert + pause/resume/cancel return Task.
* PR3: read-path — delegate reads to backend, HashMap cache, async signatures.

## Out of Scope

- Restart-time task recovery from PG (reads stay in-memory).
- NatsEventStore activation (separate dormant feature).
- Python worker task persistence.

## Technical Notes

- Pattern: `record_event_with_subject` spawn (server.rs:472-480).
- `TaskStoreBackend::update_task` is upsert — safe for fire-and-forget.
- `#[allow(dead_code)]` removed once field is read.
