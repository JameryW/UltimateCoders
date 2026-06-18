# Research: TaskStore Persistence — In-Memory to PostgreSQL Migration

- **Query**: How to migrate in-memory HashMap task store to persistent PostgreSQL storage in Rust gRPC server systems; patterns from temporal.io, celery, distributed task queues; event sourcing trade-offs
- **Scope**: Mixed (internal codebase analysis + external pattern research)
- **Date**: 2026-06-18

## Findings

### Files Found

| File Path | Description |
|---|---|
| `crates/uc-grpc/src/server.rs:159-314` | `TaskStore` — in-memory `HashMap<String, Task>` + `Vec<AgentEventType>` with submit/get/list/pause/resume/apply_update |
| `crates/uc-grpc/src/server.rs:575-592` | `GrpcServerInner` — holds `Arc<Mutex<TaskStore>>`, broadcast channel, task queue |
| `crates/uc-grpc/src/server.rs:609-712` | `GrpcServer::new()` and `GrpcServer::with_nats()` — construct TaskStore at server startup |
| `crates/uc-grpc/src/server.rs:1717-1800` | `apply_worker_update_to_store()` — inserts/updates tasks from local worker bridge |
| `crates/uc-grpc/src/server.rs:740-931` | `spawn_nats_subscriber()` — updates TaskStore from NATS messages |
| `crates/uc-engine/src/events.rs:1-348` | `EventStore` trait + `InMemoryEventStore` + `NatsEventStore` implementations |
| `crates/uc-engine/src/metadata/postgres.rs:1-370` | `PostgresMetadataStore` — existing PostgreSQL store with feature-gated dual-path pattern |
| `crates/uc-engine/src/checkpoint.rs:1-100` | `CheckpointManager` — snapshot + event replay recovery system |
| `crates/uc-types/src/agent.rs:1-195` | `Task`, `Subtask`, `TaskStatus`, `SubtaskStatus`, `AgentEventPayload` type definitions |
| `crates/uc-grpc/Cargo.toml` | uc-grpc deps — NO sqlx dependency currently |
| `crates/uc-engine/Cargo.toml` | uc-engine deps — sqlx feature-gated under `storage` |
| `crates/uc-grpc-server/src/main.rs` | Server binary — constructs LocalEngine + GrpcServer |
| `.trellis/spec/backend/database-guidelines.md` | Universal fallback pattern, dual-path read/write, three construction variants |
| `.trellis/spec/backend/taskservice-grpc-spec.md` | TaskStore design decision (Option 3: in-memory bridge) |
| `docs/architecture.md:229-239` | PostgreSQL tables listed: repos, symbols, references, agents, tasks, index_state |

### Code Patterns

#### 1. Current TaskStore Structure (server.rs:159-164)

```rust
pub struct TaskStore {
    tasks: HashMap<String, uc_types::Task>,
    events: Vec<uc_engine::AgentEventType>,
    last_heartbeat: Option<chrono::DateTime<chrono::Utc>>,
}
```

Key characteristics:
- All methods are synchronous (`&self` / `&mut self`), wrapped in `Arc<Mutex<TaskStore>>`
- `TaskStore` is in `uc-grpc` crate, which has NO sqlx dependency
- `TaskStore` is accessed via `task_store.lock().await` at every call site (10+ locations in server.rs)
- Events are stored inline (not in the separate `EventStore` trait system)
- No serialization/deserialization — objects live in memory directly

#### 2. Existing PostgresMetadataStore Pattern (postgres.rs:23-28, 73-106)

```rust
pub struct PostgresMetadataStore {
    #[cfg(feature = "storage")]
    pool: Option<Arc<PgPool>>,
    fallback: Arc<tokio::sync::RwLock<FallbackData>>,
}
```

Pattern used consistently across all stores:
- `#[cfg(feature = "storage")]` gates the real client
- `Option<Arc<PgPool>>` — None when PG unavailable
- `FallbackData` always present for graceful degradation
- Three constructors: `new(url)`, `new_fallback()`, `with_pool(pool)`
- Every method has dual path: `if let Some(pool) = &self.pool { sqlx... } else { fallback... }`
- `#[cfg(not(feature = "storage"))]` block duplicates fallback path

#### 3. EventStore Trait Pattern (events.rs:132-145)

```rust
#[async_trait::async_trait]
pub trait EventStore: Send + Sync {
    async fn append(&self, subject: &str, event: &AgentEventType) -> Result<u64, EngineError>;
    async fn read_from(&self, subject: &str, offset: u64) -> Result<Vec<RecordedEvent>, EngineError>;
    async fn latest_offset(&self, subject: &str) -> Result<u64, EngineError>;
}
```

Already has two implementations: `InMemoryEventStore` and `NatsEventStore`. The `CheckpointManager` uses `Arc<dyn EventStore>` — a trait-object pattern that allows swapping backends.

#### 4. Task Update Flow

Two paths write to TaskStore:
- **NATS path**: `spawn_nats_subscriber()` receives `uc.task.update` / `uc.task.event` / `uc.heartbeat`, locks TaskStore, mutates
- **Local worker path**: `apply_worker_update_to_store()` receives `WorkerTaskUpdate`, locks TaskStore, upserts task + records events

Both paths currently:
1. Acquire `Mutex<TaskStore>`
2. Mutate in-memory HashMap
3. Record events to inline Vec
4. Broadcast to `event_tx` channel for WatchTask streams

### External References

#### Temporal.io Persistence Model

Temporal.io uses a **hybrid event sourcing + snapshot** approach:

1. **Event sourcing as primary**: Every state transition (workflow start, activity completion, timer fired) is recorded as an event appended to a "history" table in PostgreSQL. Events are never mutated — only appended.
2. **Mutable state as cache**: Current workflow state (the "mutable state") is maintained in memory and periodically flushed to a `current_executions` table. This is a denormalized cache, not the source of truth.
3. **Snapshot for recovery**: On crash, Temporal rebuilds state by loading the mutable state row, then replaying any events after the last snapshot offset.
4. **Sharding**: History is partitioned by workflow ID (tree-based sharding). Each shard has its own event sequence.

**Key tables** (Temporal's PostgreSQL schema):
- `history_node` — event log (append-only, partitioned by shard)
- `history_tree` — branching/merge points for workflow versioning
- `current_executions` — mutable state cache (one row per active workflow)
- `tasks` — activity/timer task queues (mutable, consumed by workers)

**Relevant insight**: Temporal's "events are source of truth, mutable state is a cache" pattern is the inverse of typical CRUD. The mutable state can always be reconstructed from events. This allows temporal to guarantee exactly-once execution semantics.

#### Celery Result Backend Patterns

Celery uses a simpler model — **result backend** rather than event sourcing:

1. **Task metadata** (status, result, traceback) stored in a key-value store (Redis, PostgreSQL, MongoDB, etc.)
2. **Status transitions**: `PENDING -> STARTED -> SUCCESS/FAILURE/RETRY/REVOKED`
3. **No event log**: Only the current state is stored. History is lost unless explicitly configured (`task_track_started=True`)
4. **PostgreSQL backend**: Uses a single `celery_taskmeta` table with `task_id` (primary key), `status`, `result`, `date_done`, `traceback`
5. **No dual-write**: Celery writes directly to the result backend. In-memory state is only for the current process's active tasks.

**Relevant insight**: Celery's pattern is simpler but loses history. For an orchestration system that needs replay/recovery, event sourcing is more appropriate.

#### Rust sqlx Patterns for State Machines

Common patterns observed in Rust projects using sqlx for task/state management:

1. **sqlx::query_as for reads, sqlx::query for writes** — compile-time checked queries via `sqlx::query!` macro (requires `DATABASE_URL` at compile time) or runtime `sqlx::query()` (used in this project).

2. **JSONB columns for complex nested types** — Task/Subtask with variable fields are often stored as `JSONB` in PostgreSQL. This avoids schema migrations for type changes while still allowing indexing via GIN indexes.

3. **SERIAL/BIGSERIAL for event offsets** — Monotonically increasing sequence numbers for event ordering.

4. **Transaction-wrapped state transitions** — All status transitions wrapped in `BEGIN...COMMIT` to ensure atomicity:
   ```rust
   let mut tx = pool.begin().await?;
   sqlx::query("UPDATE tasks SET status = $1, updated_at = NOW() WHERE id = $2 AND status = $3")
       .bind(new_status).bind(task_id).bind(expected_current_status)
       .execute(&mut *tx).await?;
   sqlx::query("INSERT INTO task_events (task_id, event_type, payload) VALUES ($1, $2, $3)")
       .bind(task_id).bind(event_type).bind(payload_json)
       .execute(&mut *tx).await?;
   tx.commit().await?;
   ```

5. **Optimistic concurrency via `updated_at` or version column** — `WHERE version = $expected_version` in UPDATE statements; if affected rows = 0, another writer got there first.

#### Migration Strategies: In-Memory to Persistent

Three common strategies, in order of increasing complexity:

**Strategy A: Swap-In Replacement (simplest, recommended for this project)**

Replace `HashMap<String, Task>` with a trait `TaskStoreBackend` that has two implementations: `InMemoryTaskStore` and `PostgresTaskStore`. At server startup, choose based on configuration.

- Follows the existing `EventStore` trait pattern already in the codebase
- Follows the existing `PostgresMetadataStore` dual-path pattern
- Clean cutover: all reads/writes go through the trait
- Risk: brief downtime during cutover (acceptable for single-server deployment)

**Strategy B: Dual-Write (most complex, highest consistency)**

Write to both in-memory and PostgreSQL simultaneously. Read from in-memory (fast). PostgreSQL is the source of truth for recovery.

- Used by systems that cannot tolerate write latency (e.g., high-throughput task queues)
- Requires reconciliation logic for when one write fails
- More complex: need to handle partial failures, ordering, and eventual consistency
- Overkill for this project's throughput requirements

**Strategy C: Background Sync (least disruptive, eventual consistency)**

Keep in-memory as primary, periodically sync to PostgreSQL in the background.

- Used when the in-memory store is performance-critical and PostgreSQL is only for crash recovery
- Risk: data loss window between syncs (can be mitigated with WAL-style append)
- Reconciliation on startup: load from PostgreSQL, then accept new updates
- Appropriate if write latency to PG is a concern (not the case here)

**Recommendation for UltimateCoders**: Strategy A (swap-in replacement) aligns with existing patterns. The `EventStore` trait already demonstrates this approach works in this codebase. The `PostgresMetadataStore` shows the exact dual-path code structure to follow.

#### Event Sourcing for Task Orchestration

**Arguments FOR event sourcing** (applicable to this system):

1. **Already partially implemented**: The codebase has `AgentEventType` enum (events.rs:35-91) with `TaskCreated`, `SubtaskAssigned`, `SubtaskStarted`, `SubtaskCompleted`, `SubtaskFailed` variants. The `EventStore` trait and `CheckpointManager` already implement the core pattern.

2. **Replay and recovery**: The `CheckpointManager` (checkpoint.rs) already supports snapshot + replay. Making TaskStore event-sourced would allow full state reconstruction after crashes.

3. **Audit trail**: Every state transition is recorded. Valuable for debugging worker failures and understanding task execution history.

4. **Temporal consistency**: Events provide a total order of state changes, eliminating race conditions in concurrent updates.

**Arguments AGAINST full event sourcing**:

1. **Complexity**: Every mutation must be expressed as an event + a reducer. The current `TaskStore` methods mutate state directly (e.g., `pause_task()` sets `task.status = Paused`). Event sourcing requires splitting each mutation into "emit event" + "apply event" steps.

2. **Query overhead**: Reading current state requires either (a) maintaining a materialized view (current TaskStore pattern) or (b) replaying events from the beginning. Temporal solves this with both a history table AND a current_executions table — but this doubles storage and complexity.

3. **Schema evolution**: Event types change over time. Old events must remain deserializable. The `AgentEventType` enum uses `serde`, which handles renamed variants with `#[serde(rename)]`, but adding/removing variants requires migration logic.

4. **Sufficient alternatives**: A simple "current state table + event log table" pattern (without full event sourcing) gives most of the benefits: current state is queryable, events provide audit trail, but state transitions don't need to be derived from events.

**Pragmatic recommendation**: Use a **hybrid** approach — maintain a `tasks` table (current state, mutable) and an `agent_events` table (append-only audit log). This is the pattern Temporal actually uses internally (mutable state cache + immutable history). The existing `CheckpointManager` can write snapshots to PostgreSQL instead of `DashMap`.

### Proposed PostgreSQL Schema (based on existing types)

Based on `uc-types/src/agent.rs` types:

```sql
-- Current task state (mutable, queryable)
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    project_id TEXT NOT NULL,
    status TEXT NOT NULL,
    subtasks JSONB NOT NULL DEFAULT '[]',
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

-- Append-only event log (immutable, for replay/audit)
CREATE TABLE IF NOT EXISTS agent_events (
    id BIGSERIAL PRIMARY KEY,
    task_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload JSONB NOT NULL,
    offset BIGINT NOT NULL,
    subject TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_tasks_project_id ON tasks(project_id);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_agent_events_task_id ON agent_events(task_id);
CREATE INDEX IF NOT EXISTS idx_agent_events_subject ON agent_events(subject);
```

### Key Design Decisions for Implementation

1. **Where to place the persistent TaskStore**: In `uc-engine` (alongside `PostgresMetadataStore`), not in `uc-grpc`. This requires `uc-grpc` to depend on `uc-engine`'s `storage` feature. Currently `uc-grpc` depends on `uc-engine` with `default-features = false`.

2. **Trait vs direct struct**: The `EventStore` trait pattern (with `Arc<dyn EventStore>`) is cleaner for testability and swapping. Define a `TaskStoreBackend` trait in `uc-engine` with `InMemoryTaskBackend` and `PostgresTaskBackend` implementations.

3. **Async methods**: Current `TaskStore` methods are synchronous (operating on `HashMap`). PostgreSQL-backed methods must be async. The `Arc<Mutex<TaskStore>>` wrapper already makes all callers async (`.lock().await`), so changing `TaskStore` methods to async is a compatible change.

4. **Feature gating**: Follow the `PostgresMetadataStore` pattern — `#[cfg(feature = "storage")]` gates the `PgPool`, fallback is always available.

5. **uc-grpc Cargo.toml**: Must add `uc-engine/storage` feature or add sqlx as a direct dependency. The cleaner approach is to add a `storage` feature to `uc-grpc` that enables `uc-engine/storage`.

### Related Specs

- `.trellis/spec/backend/database-guidelines.md` — Universal fallback pattern, dual-path read/write, three construction variants
- `.trellis/spec/backend/taskservice-grpc-spec.md` — TaskStore design decision (Option 3: in-memory bridge), design decisions section
- `.trellis/spec/backend/nats-bridge-spec.md` — NATS message subjects and update flow

## Caveats / Not Found

1. **No web search was available** — External references for Temporal.io and Celery patterns are based on training knowledge, not live documentation. The Temporal schema details should be verified against the current Temporal source code if precision is critical.

2. **TaskStore events vs EventStore events are separate systems** — The `TaskStore.events: Vec<AgentEventType>` is an inline event log used for `WatchTask` streaming. The `EventStore` trait in `uc-engine` is a separate, more sophisticated system with NATS persistence. These two event systems would need to be unified or explicitly kept separate during migration.

3. **Subtask storage**: The proposed schema uses `JSONB` for subtasks (embedded in the tasks row). An alternative is a separate `subtasks` table with a foreign key to `tasks`. The JSONB approach is simpler and matches the current in-memory structure (Task contains Vec<Subtask>), but a separate table would enable independent subtask queries.

4. **Transaction semantics**: The current `Arc<Mutex<TaskStore>>` provides serialized access (one writer at a time). Moving to PostgreSQL with `sqlx` introduces concurrent access. Optimistic concurrency control (version column or status check in WHERE clause) is needed to prevent race conditions.

5. **Migration timing**: The `TaskStore` currently has no persistence at all — server restart loses all task state. This means there is no data migration required for existing data. The migration is purely a code change, not a data migration.
