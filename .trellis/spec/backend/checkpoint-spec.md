# Checkpoint / Recovery Code-Spec

> Event-sourcing snapshot + replay for task state recovery — executable contracts for implementation.

---

## Scenario: Task Checkpoint (Snapshot) Creation

### 1. Scope / Trigger

- Trigger: `CreateCheckpoint` RPC, or auto-snapshot every `snapshot_interval` events (when `CheckpointManager.record_event` is the recording path).
- Cross-layer: gRPC `TaskService::create_checkpoint` → `GrpcServer.inner.checkpoint_manager.create_snapshot(task_id)` → replays the task's event stream from the shared `EventStore` → stores a `TaskSnapshot` in the in-memory `DashMap` → appends a `CheckpointCreated` event.
- Production path: `TaskStore.record_event_with_subject` appends events to the `EventStore` under subject `task.{task_id}`; `CheckpointManager` (subject_prefix `task.`) reads the same stream.

### 2. Signatures

#### Rust Core (`crates/uc-engine/src/checkpoint.rs`)

```rust
pub struct CheckpointConfig {
    pub snapshot_interval: u64,        // events between auto-snapshots (default 100)
    pub snapshot_prefix: String,       // snapshot id prefix (default "snapshot")
    pub subject_prefix: String,        // EventStore subject prefix — default
                                       // "agent.events." (LocalEngine path);
                                       // TaskStore passes "task." so recover()
                                       // reads the same stream production events
                                       // land in.
}

pub struct CheckpointManager {
    event_store: Arc<dyn EventStore>,
    snapshot_store: DashMap<String, TaskSnapshot>,  // in-memory; TiKV out of scope
    config: CheckpointConfig,
    event_count: AtomicU64,
}

impl CheckpointManager {
    pub fn new(event_store: Arc<dyn EventStore>, config: CheckpointConfig) -> Self;
    pub async fn record_event(&self, subject: &str, event: AgentEventType) -> Result<u64, EngineError>;
    pub async fn create_snapshot(&self, task_id: &str) -> Result<String, EngineError>;  // -> snapshot_id
    pub async fn recover(&self, task_id: &str) -> Result<TaskSnapshot, EngineError>;
    pub async fn list_events(&self, task_id: &str, from_offset: u64) -> Result<Vec<RecordedEvent>, EngineError>;
    fn subject_for(&self, task_id: &str) -> String;  // {subject_prefix}{task_id}
}
```

#### Shared Types (`crates/uc-types/src/agent.rs`)

```rust
pub struct TaskSnapshot {
    pub task_id: String,
    pub status: String,
    pub subtasks: Vec<SubtaskSnapshot>,
    pub last_event_offset: u64,
    pub timestamp: i64,  // Unix ms
}
pub struct SubtaskSnapshot {
    pub subtask_id: String,
    pub status: String,
    pub assigned_worker: Option<String>,
    pub result_summary: Option<String>,
}
```

> Moved from `uc-engine/src/events.rs` to `uc-types/src/agent.rs` so the `EngineApi` trait (in uc-types) can return them. `uc-engine` re-exports.

#### EngineApi Trait (`crates/uc-types/src/engine.rs`)

```rust
async fn checkpoint_task(&self, task_id: &str) -> Result<String, EngineError> { /* default: InvalidOperation */ }
async fn recover_task(&self, task_id: &str) -> Result<TaskSnapshot, EngineError> { /* default: InvalidOperation */ }
```

> Default impls return `InvalidOperation` — only engines with a `CheckpointManager` implement them (LocalEngine). Mirrors the scheduler-method pattern.

#### gRPC (`crates/uc-grpc/`)

```proto
service TaskService {
    rpc CreateCheckpoint(CreateCheckpointRequest) returns (CreateCheckpointResponse);
    rpc RecoverTask(RecoverTaskRequest) returns (RecoverTaskResponse);
}
```

- `GrpcServerInner.checkpoint_manager: Arc<CheckpointManager>` — built in all 4 constructors sharing `TaskStore`'s `EventStore`.
- `GrpcServer::checkpoint_manager()` getter.
- Client: `GrpcEngineClient::create_checkpoint` / `recover_task` (inherent; TaskService is not part of `EngineApi`).

### 3. Data Flow

```
TaskStore.record_event_with_subject(event, "task.{id}")
  ├─ inline log (capped, recent replay)
  └─ EventStore.append("task.{id}", event)        ← spawned, fire-and-forget
                                                    (shared Arc — same store
                                                     CheckpointManager reads)

CreateCheckpoint RPC
  → checkpoint_manager.create_snapshot(id)
      → EventStore.read_from("task.{id}", 0)       ← replay all events
      → collect_subtask_states (replay)
      → snapshot_store.insert(snapshot_id, snapshot)
      → EventStore.append("task.{id}", CheckpointCreated)

RecoverTask RPC
  → checkpoint_manager.recover(id)
      → find_latest_snapshot(id)
      → EventStore.read_from("task.{id}", snap.last_event_offset + 1)
      → apply_event_to_snapshot (replay post-snapshot events)
      → TaskSnapshot
```

### 4. Constraints

- **Subject-prefix alignment is load-bearing.** `TaskStore` records under `task.{id}`; `CheckpointManager` MUST be constructed with `subject_prefix: "task."`. A mismatch makes `recover()` read an empty stream and silently return a zero-subtask snapshot. The `build_checkpoint_manager` helper enforces this.
- **Snapshot store is in-memory `DashMap`.** Lost on restart. TiKV/external snapshot store is out of scope (MVP). Recovery from scratch (no snapshot) still works via full event replay.
- **EventStore append is fire-and-forget** (`tokio::spawn` in `record_event_with_subject`). Tests that checkpoint immediately after recording MUST yield (`tokio::time::sleep`) to let the spawned append land.
- **Full Task reconstruction from snapshot is lossy** — `TaskSnapshot` has no `depends_on` / `file_constraints` / `agent_config_json`. Do not rewrite `TaskStore` from a snapshot; use it for diagnostics/drift only.

### 5. Resume Integration (advisory recover)

`resume_task` RPC calls `checkpoint_manager.recover(task_id)` after a successful in-memory resume and logs drift (recovered subtask status ≠ TaskStore subtask status) at WARN. Advisory only — does not rewrite TaskStore.

```
resume_task
  → store.resume_task(id)              ← in-memory status flip (Paused → InProgress)
  → checkpoint_manager.recover(id)     ← best-effort
  → if recovered.subtasks drift from task.subtasks: tracing::warn!(drift_count, ...)
  → broadcast TaskResumed + NATS publish
```

**Upgrade path:** reconstruct `Task` from snapshot when `TaskStore` loses a task on restart (needs richer snapshot fields — `depends_on`, `file_constraints`, `agent_config_json`).

### 6. Failure Modes

| Failure | Behavior |
|---|---|
| `recover()` with no events | Returns `TaskSnapshot` with `status: "created"`, 0 subtasks (replay from scratch yields empty state) |
| `recover()` EventStore error | Propagated as `EngineError` → RPC returns `success: false` |
| `create_snapshot()` with no events | `latest_offset: 0`, empty subtasks, snapshot still created |
| `resume_task` recover fails | Logged, resume still succeeds (recover is best-effort) |

### 7. Out of Scope

- TiKV / external snapshot store (in-memory DashMap only).
- Python worker checkpoint integration.
- OMP UI for checkpoint inspection / manual recover trigger.
- Snapshot compaction / GC (snapshots accumulate in the DashMap).
- Full `Task` reconstruction from snapshot on TaskStore loss.
