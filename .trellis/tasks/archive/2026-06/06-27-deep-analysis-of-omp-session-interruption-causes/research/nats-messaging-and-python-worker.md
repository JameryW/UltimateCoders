# Research: NATS Messaging and Python Worker Interruption Causes

- **Query**: NATS message delivery, subscription lifecycle, Python worker connection, heartbeat failures
- **Scope**: Internal
- **Date**: 2026-06-27

## Findings

### Files Found

| File Path | Description |
|---|---|
| `python/ultimate_coders/nats_worker.py` | NATS worker: bridges gRPC TaskService with Python Orchestrator |
| `crates/uc-grpc/src/server.rs` | Rust NATS subscriber, heartbeat monitor, dedup logic |
| `packages/uc-orchestrator/src/orchestrator/control-signal-subscriber.ts` | TS NATS client for control signals |

### Code Patterns

#### 1. NATS Connection with Retry (Python)

`nats_worker.py` `_connect_with_retry()`:
- 5 attempts with exponential backoff (2s, 4s, 8s, 16s, 32s)
- Raises `ConnectionError` if all attempts fail
- **No ongoing reconnect**: Once connected, if the NATS connection drops, the `nats.py` client has built-in reconnect, but the subscriber loop may exit

#### 2. NATS Message Delivery Guarantees

The system uses at-least-once delivery with dedup:

- **Dedup keys**: Every message includes `message_id` field formatted as `{task_id}:{event_type}:{subtask_id}:{5s_bucket}`
- **Rust dedup**: `TaskStore.check_and_record_message_id()` with 5-minute TTL, max 10K entries
- **Python dedup**: `ControlSignalSubscriber.seenMessageIds` with 5-minute TTL, max 10K entries
- **JetStream**: Event Sourcing with `UC_TASK_EVENTS` stream, interest-based retention, 7-day max age, 2-minute duplicate window

**Message loss scenarios**:
- If NATS server is down when a message is published, the publish is fire-and-forget (`_publish()` catches all exceptions and logs warning)
- If the Rust subscriber is not running (e.g., during gRPC server restart), messages accumulate in NATS but the in-memory TaskStore won't receive them until the subscriber reconnects
- If the Python worker crashes mid-task, the heartbeat stops, and after 600s the Rust heartbeat monitor marks all InProgress tasks as Failed

#### 3. Heartbeat Monitoring

**Rust side** (`spawn_heartbeat_monitor` in server.rs):
- Checks every 30s
- Default timeout: 600s (10 min)
- Two levels:
  1. **Consumer heartbeat**: If no heartbeat from Python NATS consumer within timeout, marks ALL InProgress/Planning tasks as Failed
  2. **Worker heartbeat**: Per-worker tracking. Stale workers (> timeout) have their subtasks reassigned to Pending and re-dispatched

**Python side** (`_heartbeat_loop` in nats_worker.py):
- Publishes heartbeat every 30s
- Includes worker info (worker_id, capabilities, load, capacity)
- Failure to publish is logged as warning but doesn't stop the loop

**Python stale worker cleanup** (`_stale_worker_cleanup_loop`):
- Runs every 60s
- Removes remote workers with no heartbeat for >90s
- Reassigns their in-progress subtasks back to Pending
- Also detects local worker heartbeat stall (>90s) and releases the current subtask

#### 4. NATS Subscription Failure Modes

**Rust subscriber** (server.rs `spawn_nats_subscriber`):
- Subscribes to three subjects: `uc.task.update`, `uc.task.event`, `uc.heartbeat`
- If ANY initial subscription fails, logs warning and returns (entire subscriber exits)
- The `tokio::select!` loop exits when ALL subscriptions return `None` (stream end)
- **No re-subscription**: If the subscriber exits, there is no mechanism to restart it

**Python subscriber** (nats_worker.py):
- Subscribes to `uc.task.submit`, `uc.heartbeat`, `uc.dashboard.>`, `uc.task.event`
- No explicit error handling for subscription drops -- relies on `nats.py` client's built-in reconnect
- On `stop()`: unsubscribes all, drains NATS connection

**TypeScript subscriber** (control-signal-subscriber.ts):
- Single subscription to `uc.task.event`
- 2s connection timeout
- Falls back to gRPC polling (every 2s) if NATS unavailable
- **Polling creates a new GrpcBridge each interval** -- no connection reuse in polling mode

#### 5. Event-Driven Dispatch

The Python worker uses `asyncio.Event` for event-driven subtask dispatch:
- `_dispatch_event`: Set when a subtask completes/fails (via `_handle_task_event`)
- `_execute_subtasks()` waits on this event with 30s safety timeout to prevent deadlock
- If the NATS event doesn't arrive (message loss), the 30s timeout triggers a re-evaluation of ready subtasks

### Caveats / Not Found

- No explicit NATS JetStream consumer durability configuration beyond the `dashboard-replay` consumer
- The Python NATS worker has no built-in mechanism to detect that the Rust gRPC server has restarted
- The `_execute_subtasks()` safety limit (`max_iterations = len(task.subtasks) * 2 + 1`) could be reached if subtasks keep failing and getting retried
- No backpressure mechanism -- if the Rust server publishes many events faster than the Python worker can process, messages queue up in NATS
