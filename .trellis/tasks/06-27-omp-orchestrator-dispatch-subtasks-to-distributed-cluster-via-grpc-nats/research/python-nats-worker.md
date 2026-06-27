# Research: Python NatsWorker

- **Query**: How does the worker receive and execute subtasks? What's the worker lifecycle? Heartbeat? Result reporting?
- **Scope**: internal
- **Date**: 2026-06-27

## Findings

### Files Found

| File Path | Description |
|---|---|
| `python/ultimate_coders/nats_worker.py` | Main NatsWorker + NatsPublisher (1641 lines) |
| `python/ultimate_coders/agent/worker.py` | Worker class (sandbox execution, checkpoint, heartbeat) |
| `python/ultimate_coders/agent/orchestrator.py` | Minimal Orchestrator (task CRUD, worker registration, subtask selection) |
| `python/ultimate_coders/agent/types.py` | Task/Subtask/SubtaskResult/WorkerInfo types |

### NatsWorker Modes

The NatsWorker supports two modes, controlled by `--mode` CLI flag (line 1597):

| Mode | Subscriptions | Purpose |
|------|--------------|---------|
| `default` | `uc.task.submit`, `uc.heartbeat`, `uc.dashboard.>`, `uc.task.event` | Full Orchestrator consumer (decomposition + execution) |
| `worker` | `uc.subtask.execute` (queue group `workers`) | Distributed worker only (receives + executes subtasks) |

### Worker Lifecycle

**Start** (`start()` method, line 294):
1. Connect to NATS with retry (exponential backoff, 5 attempts)
2. Set up JetStream stream `UC_TASK_EVENTS` + durable consumer `dashboard-replay`
3. Initialize Engine, Orchestrator, Worker components
4. Replay missed events from JetStream (default mode only)
5. Subscribe based on mode (default vs worker)
6. Start heartbeat loop (every 30 seconds)
7. Start dashboard snapshot loop (default mode, every 5 seconds)
8. Start stale worker cleanup loop (default mode, every 60 seconds)

**Stop** (`stop()` method, line 382):
1. Cancel all background tasks (heartbeat, snapshot, cleanup)
2. Unsubscribe all NATS subscriptions
3. Drain and close NATS connection

### Heartbeat

- Published every 30 seconds via `_heartbeat_loop()` (line 976)
- Payload includes `consumer_id`, `worker_id`, `capabilities`, `current_load`, `max_capacity`, `pending_subtask_count`
- Published to `uc.heartbeat` subject
- Also refreshes heartbeat on Orchestrator side via `orchestrator.refresh_heartbeat()`

### Subtask Execution (Worker Mode)

`_handle_subtask_execute()` (line 1015):
1. Parse `uc.subtask.execute` message JSON
2. Extract `task_id`, `subtask_id`, `description`, `timeout_seconds`
3. Build a `Subtask` object from the message data
4. Call `self._worker.execute_subtask(subtask)` -- this runs the subtask in a sandbox (Claude Code / Codex)
5. On success: publish result via `uc.task.update` with status "Completed"
6. On failure: publish result via `uc.task.update` with status "Failed"
7. Result is published using `_make_subtask_result_task()` which constructs a minimal Task object with just the result subtask

### Subtask Execution (Default Mode)

`_execute_subtasks()` (line 780):
1. Event-driven loop that iterates until task is complete/failed/paused
2. Each iteration:
   - Refresh task state (may have changed via NATS events)
   - Collect all ready subtask IDs (pending, dependencies met)
   - Check if remote workers are available via `_has_remote_workers()`
   - If remote workers exist: dispatch via `_dispatch_remote()` (publishes to `uc.subtask.execute`)
   - If no remote workers: execute locally via `_run_one()` (assign, execute, report)
   - Uses `asyncio.gather()` for concurrent local execution (bounded by worker capacity)
3. Event-driven wake-up: `_dispatch_event` asyncio.Event is set when a subtask completes/fails via `uc.task.event`, waking the loop immediately

### Remote Dispatch

`_dispatch_remote()` (line 937):
1. Assigns subtask to "remote" worker in local Orchestrator
2. Declares edit intent for conflict tracking
3. Publishes `uc.subtask.execute` message to NATS (NOT using queue group on the publish side)
4. Results come back via `uc.task.event` (subtask_completed/subtask_failed)

### Remote Worker Discovery

- `_handle_heartbeat()` (line 1214) subscribes to `uc.heartbeat` and tracks remote workers in `_known_remote_workers` dict
- `_has_remote_workers()` (line 1246) returns True if any remote workers are known
- `_stale_worker_cleanup_loop()` (line 1250) removes workers with no heartbeat for >90s and reassigns their subtasks back to Pending

### JetStream Event Sourcing

- Stream `UC_TASK_EVENTS` on subject `uc.task.event` with interest-based retention, 7-day max age, 2-min dedup window
- Durable consumer `dashboard-replay` for replay after restart
- `_replay_missed_events()` (line 498) replays up to 500 events from last acked sequence
- Sequence persisted to engine memory (`event_sourcing` scope, key `js_last_seq`)

### NatsPublisher

`NatsPublisher` class (line 172) wraps NATS publish with JSON serialization:
- `publish_update(task)` -- publishes to `uc.task.update`
- `publish_event(event_type, task_id, ...)` -- publishes to `uc.task.event`
- `publish_heartbeat(consumer_id, ...)` -- publishes to `uc.heartbeat`
- `publish_submit(task_id, description, ...)` -- publishes to `uc.task.submit`

All payloads include `message_id` for deduplication with 5-second bucketing.

## Caveats / Not Found

- The worker mode (`--mode worker`) does NOT subscribe to `uc.task.submit` -- it only receives subtask execution requests. It cannot decompose tasks.
- The default mode NatsWorker does BOTH decomposition AND execution. It can also dispatch to remote workers if they are discovered via heartbeats.
- The `_dispatch_remote()` method publishes to `uc.subtask.execute` without using a queue group -- the queue group is only on the subscriber side (workers subscribe with `queue="workers"`)
- There is no explicit "registration" message from worker to server -- workers are discovered implicitly via heartbeats on `uc.heartbeat`
