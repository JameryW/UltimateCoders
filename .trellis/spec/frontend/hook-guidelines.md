# Callback and Event Patterns

> How events are sourced, callbacks are used, and checkpoint/resume works.

---

## Overview

The project uses an **Event Sourcing** pattern in the Rust core and a **callback-driven** orchestration pattern in the Python layer. Together they provide audit, replay, recovery, and real-time progress tracking.

---

## Event Sourcing (Rust Core)

All agent actions are recorded as events for audit, replay, and recovery. The `EventStore` trait abstracts the storage backend (NATS JetStream or in-memory for testing).

### AgentEventType Enum

Defined in `crates/uc-engine/src/events.rs:34-91`. Every significant action is recorded:

| Event | Fields | When Emitted |
|-------|--------|-------------|
| `TaskCreated` | `task_id`, `description` | New task submitted |
| `SubtaskAssigned` | `subtask_id`, `worker_id` | Subtask assigned to a worker |
| `SubtaskStarted` | `subtask_id`, `worker_id` | Worker begins execution |
| `ToolInvoked` | `subtask_id`, `tool_name`, `tool_input` | Worker calls a tool |
| `ToolResult` | `subtask_id`, `tool_output`, `success` | Tool returns a result |
| `FileModified` | `subtask_id`, `file_path`, `diff` | Worker modifies a file |
| `EditIntent` | `worker_id`, `file_path`, `edit_type`, `regions` | Worker declares intent to edit |
| `SubtaskCompleted` | `subtask_id`, `summary`, `success` | Subtask finishes |
| `SubtaskFailed` | `subtask_id`, `error`, `recoverable` | Subtask fails |
| `CheckpointCreated` | `task_id`, `snapshot_offset` | Snapshot taken |

### EventStore Trait

The trait abstracts append and replay:

```rust
#[async_trait]
pub trait EventStore: Send + Sync {
    async fn append(&self, subject: &str, event: &AgentEventType) -> Result<u64, EngineError>;
    async fn replay(&self, subject: &str, after_offset: u64) -> Result<Vec<RecordedEvent>, EngineError>;
    // ...
}
```

`InMemoryEventStore` is the fallback implementation for testing.

---

## Checkpoint/Resume Pattern

The `CheckpointManager` (`crates/uc-engine/src/checkpoint.rs:44-50`) combines event sourcing with periodic snapshots:

```rust
pub struct CheckpointManager {
    event_store: Arc<dyn EventStore>,
    snapshot_store: DashMap<String, TaskSnapshot>,  // In-memory fallback for snapshots
    config: CheckpointConfig,
    event_count: AtomicU64,
}
```

### Recovery Flow

1. Load latest snapshot from memory store
2. Replay events after the snapshot's `last_event_offset`
3. Apply events to reconstruct current state

### Auto-Snapshot

Snapshots are created automatically every `snapshot_interval` events (`crates/uc-engine/src/checkpoint.rs:67-80`):

```rust
pub async fn record_event(&self, subject: &str, event: AgentEventType) -> Result<u64, EngineError> {
    let offset = self.event_store.append(subject, &event).await?;
    let count = self.event_count.fetch_add(1, Ordering::SeqCst) + 1;

    // Auto-snapshot at configured interval
    if count.is_multiple_of(self.config.snapshot_interval) {
        if let Some(task_id) = extract_task_id(&event) {
            if let Err(e) = self.create_snapshot(&task_id).await {
                // Best-effort; log but don't fail
            }
        }
    }
    Ok(offset)
}
```

Default `snapshot_interval` is 100 events.

---

## Python Callback Pattern

### Orchestrator Callbacks

The Orchestrator uses internal method callbacks rather than external callback registration. State transitions happen through method calls that update in-memory state and persist to engine memory:

| Method | State Transition | Memory Persist |
|--------|-----------------|---------------|
| `submit_task()` | CREATED -> PLANNING -> IN_PROGRESS | Writes task definition to memory |
| `assign_subtask()` | PENDING -> ASSIGNED | Writes assignment to memory |
| `handle_subtask_result()` | ASSIGNED -> COMPLETED/FAILED | Writes result to memory |

### Error Handling in Callbacks

Memory persistence failures are logged but do not fail the operation (`python/ultimate_coders/agent/orchestrator.py:149-161`):

```python
if self.engine is not None:
    try:
        self.engine.write_memory(...)
    except Exception:
        logger.warning("Failed to write task to memory", exc_info=True)
```

This follows the same best-effort pattern as the Rust core.

---

## Conflict Detection as Event Pattern

The `EditIntent` event type enables conflict detection. Workers declare their intent to edit a file/region before making changes:

```python
# Python-side: orchestrator.py registers edit intents
from ultimate_coders.agent.conflict import ConflictDetector, EditIntent, EditType, LineRange

detector = ConflictDetector()
intent = EditIntent(
    worker_id="w1",
    file_path="src/main.rs",
    edit_type=EditType.MODIFY,
    regions=[LineRange(start=10, end=20)],
)
result = detector.check_conflict(intent)
```

The `ConflictDetector` tracks active edit intents and detects overlapping regions.

---

## LineRange Overlap Detection

`LineRange` (`crates/uc-engine/src/events.rs:14-28`) uses inclusive-start, exclusive-end ranges with overlap detection:

```rust
pub struct LineRange {
    pub start: u32,
    pub end: u32,
}

impl LineRange {
    pub fn overlaps(&self, other: &LineRange) -> bool {
        self.start < other.end && other.start < self.end
    }
}
```

---

## Common Mistakes

1. **Not persisting state transitions to memory** -- Every significant state change (task creation, assignment, result) should be written to engine memory for durability. Without this, state is lost if the orchestrator restarts.

2. **Propagating memory write failures** -- Memory persistence is best-effort. Log the failure but do not fail the state transition. The primary state is in-memory; memory is a durability layer.

3. **Forgetting to declare edit intents** -- Workers that modify files without declaring `EditIntent` will not be tracked by the conflict detector, leading to undetected conflicts.
