# Research: Architecture Gaps — TUI/OMP Unified Control Path

- **Query**: How do TUI, Dashboard, UCOrchestrator, and gRPC server connect? Where are the control-path gaps for submit/pause/resume/cancel?
- **Scope**: internal
- **Date**: 2026-06-25

## Findings

### Files Found

| File Path | Description |
|---|---|
| `tui/src/grpc/client.ts` | TUI gRPC client — native `@grpc/grpc-js` connecting to `localhost:50051` |
| `tui/src/grpc/types.ts` | TUI proto type definitions + status mappers |
| `tui/src/hooks/useGrpcClient.ts` | TUI React hook — connection lifecycle, submit/pause/resume/listTasks |
| `tui/src/hooks/useTaskEvents.ts` | TUI React hook — WatchTask server-stream subscription, event processing |
| `tui/src/reducer.ts` | TUI state reducer — single source of truth for all TUI state |
| `tui/src/components/App.tsx` | TUI root component — wires hooks to reducer |
| `dashboard/src/hooks/useGrpcWeb.ts` | Dashboard gRPC-Web hook — WatchTask + WatchDashboard streams, submit/pause/resume |
| `dashboard/src/hooks/useDashboardGrpc.ts` | Dashboard gRPC-Web hook for DashboardService — WatchDashboard stream, SSE fallback |
| `dashboard/src/hooks/useDashboard.ts` | Dashboard state management — handleTaskEvent, mergeGrpcTasks, optimistic updates |
| `dashboard/src/api/endpoints.ts` | Dashboard file browser API — gRPC-Web EngineService calls |
| `packages/uc-orchestrator/src/orchestrator/orchestrator.ts` | UCOrchestrator — task lifecycle, wave execution, control flow |
| `packages/uc-orchestrator/src/orchestrator/grpc-bridge.ts` | GrpcBridge — HTTP+JSON bridge from orchestrator to gRPC server |
| `packages/uc-orchestrator/src/orchestrator/task-store.ts` | TaskStore — local JSON file persistence (.uc/tasks/) |
| `packages/uc-orchestrator/src/uc-rpc-server.ts` | JSONL stdio RPC server — stdin/stdout bridge for Python OmpBridge |
| `crates/uc-grpc/src/server.rs` | Rust gRPC server — TaskService + EngineService impl, TaskStore, event broadcast |
| `crates/uc-grpc/src/dashboard_service.rs` | DashboardService impl — NATS passthrough to Python Orchestrator |
| `crates/uc-grpc/src/local_worker.rs` | LocalWorkerBridge — Python subprocess via JSON-RPC stdin/stdout |

---

## 1. TUI (Ink) Architecture

### Connection to gRPC Server
- **Protocol**: Native gRPC via `@grpc/grpc-js` + `@grpc/proto-loader` (dynamic proto loading)
- **Address**: `localhost:50051` (configurable via `GRPC_SERVER_ADDR` env var)
- **Proto path**: Resolved from `../../crates/uc-grpc/proto/engine.proto` or `GRPC_PROTO_PATH` env var
- **Client class**: `TaskServiceClient` wraps a `grpc.Client` with methods: `submitTask`, `getTask`, `listTasks`, `watchTask`, `pauseTask`, `resumeTask`

### Connection Lifecycle (`useGrpcClient.ts`)
- Auto-connect on mount
- Connectivity probe: `listTasks({})` with 3s timeout
- Exponential backoff reconnection: 1/2/4/8/16s intervals, max 5 retries
- Manual reconnect via Ctrl+R
- Connection states: `disconnected | connecting | connected | error`

### Task State Hooks (`useTaskEvents.ts`)
- Subscribes to `WatchTask` server-streaming RPC with `taskId: ''` (watch all tasks)
- Processes events via `processEvent()` function which updates a `Map<string, SubtaskItem>`
- Event types handled: `task_submitted`, `subtask_assigned`, `subtask_started`, `subtask_completed`, `subtask_failed`
- Handles `sync_required` events (broadcast lag) via callback
- Event batching: 50ms batch window for low-priority events; high-priority events (status transitions) bypass buffer
- Dedup: content-key + 1s window dedup for status-transition events
- Auto-detects when all subtasks reach terminal state and sets `isStreaming=false`
- Stream retry: max 3 retries with 5s delay on stream end/error
- Stale event filtering: discards events with timestamps before stream session start

### Submit/Pause/Resume Flow
- **Submit**: `useGrpcClient.submitTask({description, projectId})` → gRPC `SubmitTask` RPC → response includes `taskId, status, subtaskCount, subtasks`
- **Pause**: `useGrpcClient.pauseTask({taskId})` → gRPC `PauseTask` RPC
- **Resume**: `useGrpcClient.resumeTask({taskId})` → gRPC `ResumeTask` RPC
- **Cancel**: Local-only via reducer (`CANCEL_TASK` action) — sets `taskCancelled=true` in TUI state. No gRPC cancel RPC exists.
- **No cancel RPC**: The TUI has no way to send a cancel command to the gRPC server. The `CANCEL_TASK` reducer action only sets local state.

### TUI Reducer State (`reducer.ts`)
- Single source of truth via `useReducer(tuiReducer, INITIAL_TUI_STATE)`
- `activeTaskId`, `subtasks`, `progress`, `isSubmitting`, `taskCancelled`
- `SYNC_TASKS` action: reconciles local subtask state with server data after reconnect
- The reducer does NOT directly call gRPC — it only manages local state

---

## 2. Dashboard Architecture

### Connection to gRPC Server
- **Protocol**: gRPC-Web via `@connectrpc/connect-web` (HTTP+protobuf, browser-compatible)
- **Address**: Same-origin (empty `VITE_GRPC_WEB_ADDR`) — Vite proxy in dev, reverse proxy in prod
- **Shared transport**: Module-level `getSharedTransport()` — single HTTP/2 connection reused by all gRPC-Web consumers
- **Auth**: Bearer token from `localStorage.getItem("uc_dashboard_token")`

### Two Streaming Paths
1. **TaskService.WatchTask** (`useGrpcWeb.ts`): Same WatchTask RPC as TUI, receives fine-grained task events
2. **DashboardService.WatchDashboard** (`useDashboardGrpc.ts`): Receives full snapshots + incremental events

### WatchDashboard Stream (`useDashboardGrpc.ts`)
- Primary: gRPC-Web `WatchDashboard` stream via `@connectrpc/connect`
- Fallback: SSE (`EventSource("/dashboard/api/stream")`) after 5 consecutive gRPC failures
- SSE events: `task_event` (fine-grained) and `update` (full snapshot)
- Exponential backoff: 1/2/4/8/16/30/60s intervals
- Handles `sync_required` events

### DashboardService RPCs (via NATS passthrough)
- `ListWorkers` → NATS request-reply to `uc.dashboard.ListWorkers`
- `GetSchedulerStatus` → NATS request-reply to `uc.dashboard.GetSchedulerStatus`
- `GetCircuitBreakerStatus` → NATS request-reply to `uc.dashboard.GetCircuitBreakerStatus`
- `ResetCircuitBreaker`, `TriggerSchedulerJob`, `FlushPendingTasks`, `ListEvents`
- All go through `nats_dashboard_request()` which sends NATS request-reply with 5s timeout
- **When NATS unavailable**: Returns empty/default responses (graceful degradation)

### Dashboard State Management (`useDashboard.ts`)
- `handleTaskEvent()`: Processes real-time events, updates task/subtask state
- `mergeGrpcTasks()`: Field-level merge preserving incremental subtask updates (subtask status rank comparison)
- `optimisticAddTask()`: Insert task before event arrives
- `optimisticStatusUpdate()`: Immediate status update before event confirms
- Dedup: 5s window dedup on `${type}:${task_id}:${subtask_id}:${timestamp}`
- Interaction log: Per-task event log, max 50 entries (LRU eviction)

---

## 3. UCOrchestrator Architecture

### Task Lifecycle
- **Submit**: `submitTask(description, ctx)` → creates TaskState → `decompose()` → `buildDAG()` → `splitWavesByFileOverlap()` → `executeWaves()`
- **Two-phase submit** (for RPC server): `createTask(description)` returns taskId immediately, then `runTask(taskId, ctx)` runs async
- **Wave execution**: Iterates waves, checks `controlState` before each wave (paused/cancelled), executes subtasks with `maxConcurrency` workers
- **Decomposition**: Calls `runSubprocess()` with decomposer agent (LLM call)
- **Subtask execution**: `executeSubtaskWithRetry()` → `executeSubtask()` → `runSubprocess()` with worker agent
- **Review**: Optional supervisor agent review of subtask results

### Control Flow: Pause/Resume/Cancel
- **Pause** (`pauseTask()`):
  1. Sets `task.controlState = "paused"` in local Map
  2. Persists to local JSON via `TaskStore.save()`
  3. Calls `this.bridge.pauseTask(taskId)` (fire-and-forget to gRPC)
  4. Actual pause happens at next wave boundary in `executeWaves()` (checks `controlState === "paused"`)
  5. Sets `task.resumeFromWave = waveIdx` for later resume
  6. Returns immediately — does NOT wait for wave to finish

- **Resume** (`resumeTask()`):
  1. Sets `task.controlState = "running"`, `task.status = "in_progress"`
  2. Calls `this.bridge.resumeTask(taskId)` (fire-and-forget to gRPC)
  3. Resets failed subtasks to pending
  4. Rebuilds waves from pending/running subtasks
  5. Calls `executeWaves()` with remaining waves
  6. **Blocking**: `resumeTask()` is async and blocks until execution completes

- **Cancel** (`cancelTask()`):
  1. Sets `task.controlState = "cancelled"`, `task.status = "cancelled"`
  2. Calls `abortCtrl.abort()` to abort running subtasks
  3. Marks running/pending/reviewing subtasks as cancelled
  4. Cascades cancel to downstream dependent subtasks
  5. Persists and syncs to gRPC

### gRPC Sync (`syncTaskToGrpc()`)
- Called after every state change: decomposition, wave completion, subtask completion, pause, resume, cancel
- Fire-and-forget: `this.bridge.upsertTask(this.toPersisted(task)).catch(() => {})`
- **Best-effort**: gRPC sync failure is non-fatal, logged but not retried
- Uses `GrpcBridge.upsertTask()`: checks if task exists on server, calls `UpdateTask` if yes, `SubmitTask` if no

### Persistence
- **Primary**: Local JSON files in `.uc/tasks/{taskId}.json` via `TaskStore`
- **Checkpoint**: `.uc/checkpoints/{taskId}.snap.json` after each wave
- **Restore**: On startup, loads recoverable tasks (planning/in_progress/failed/paused) from disk
- **Dual storage**: Local file (primary, reliable) + gRPC sync (secondary, best-effort)

---

## 4. uc-rpc-server (JSONL Stdio Bridge)

### Protocol
- Reads JSONL from stdin, writes JSONL to stdout
- Request: `{"method": "<name>", "params": {...}, "id": <int>}`
- Response: `{"id": <int>, "result": {...}}` or `{"id": <int>, "error": {"code": ..., "message": "..."}}`
- Event: `{"event": "<type>", "data": {...}}`
- Startup: `{"event": "ready"}`

### Methods
- `submit_task`: Creates task via `orchestrator.createTask()`, then fire-and-forget `orchestrator.runTask()`
- `cancel_task`: Calls `orchestrator.cancelTask(taskId, subtaskId?)`
- `pause_task`: Calls `orchestrator.pauseTask(taskId)`
- `resume_task`: Calls `orchestrator.resumeTask(taskId)`
- `show_status` / `get_task` / `list_tasks`: Read-only queries
- `shutdown`: `process.exit(0)`

### Key Detail
- The RPC server creates a `UCOrchestrator` with a `GrpcBridge` but **no omp context** — uses `stubContext()` for all operations
- The orchestrator's `runTask()` is fire-and-forget from the RPC server's perspective
- **No event forwarding**: The RPC server does NOT emit task events back to the caller. It only returns the initial `task_id` for `submit_task` and boolean results for control commands.

---

## 5. Rust gRPC Server Architecture

### TaskStore (In-Memory)
- `tasks: HashMap<String, uc_types::Task>` — the authoritative server-side task state
- `events: Vec<AgentEventType>` — inline event log
- `event_store: Arc<dyn EventStore>` — unified EventStore for persistence
- `task_backend: Option<Arc<dyn TaskStoreBackend>>` — optional async backend (PostgreSQL)
- Dedup map for NATS at-least-once delivery (5-minute TTL)

### TaskService RPCs
- **SubmitTask**: 
  - With NATS: Creates Planning task, publishes to `uc.task.submit`, awaits Python Orchestrator
  - Without NATS: Delegates to `submit_task_via_bridge()` → LocalWorkerBridge → Python subprocess
  - No Rust-side fallback decomposition
- **GetTask / ListTasks**: Read from in-memory TaskStore
- **WatchTask**: Server-streaming RPC
  - Phase 1: Replay existing events from TaskStore (skipped for "watch all" / empty taskId)
  - Phase 2: Subscribe to `broadcast::Sender<TaskEvent>` for live events
  - Dedup: Skip events already replayed in Phase 1
  - On broadcast lag: Emits `sync_required` event
- **PauseTask**: Updates TaskStore, records `TaskPaused` event, broadcasts, publishes NATS `task_paused` event
- **ResumeTask**: Updates TaskStore, records `TaskResumed` event, broadcasts, publishes NATS `task_resumed` event
- **UpdateTask**: Full upsert on subtasks, records transition events, broadcasts

### Event Broadcast
- `event_tx: broadcast::Sender<TaskEvent>` — channel capacity 256
- All event sources publish here: local worker, NATS subscriber, local decomposition
- WatchTask streams subscribe via `event_tx.subscribe()`
- On lag: `broadcast::RecvError::Lagged(n)` → emit `sync_required` event

### NATS Integration (feature-gated)
- `uc.task.submit`: gRPC → Python (task submission)
- `uc.task.update`: Python → gRPC (status updates, full upsert)
- `uc.task.event`: Python → gRPC (fine-grained events: tool_call, tool_result, file_modified, etc.)
- `uc.heartbeat`: Python → gRPC (consumer heartbeat)
- `uc.subtask.execute`: gRPC → Worker queue (subtask dispatch)
- Background subscriber: `spawn_nats_subscriber()` — updates TaskStore from `uc.task.update` and `uc.task.event`
- Heartbeat monitor: `spawn_heartbeat_monitor()` — marks stale tasks as Failed

### LocalWorkerBridge
- Spawns `python -m ultimate_coders.local_worker` as subprocess
- Communicates via JSON-RPC 2.0 over stdin/stdout
- Background notification reader: applies `WorkerTaskUpdate` and `WorkerTaskEvent` to TaskStore
- Auto-restart on crash with exponential backoff (max 3 retries)
- On worker death: marks all in-progress tasks as Failed

### DashboardService
- All RPCs forward via NATS request-reply to Python Orchestrator
- `WatchDashboard`: Dual stream — incremental events from `uc.task.event` + periodic snapshots from `uc.dashboard.snapshot`
- Without NATS: Builds snapshots from local TaskStore + event_rx

---

## 6. Event Flow Analysis

### Current Event Flow: Orchestrator → gRPC Server → TUI/Dashboard

**Path A: Orchestrator → gRPC (via GrpcBridge HTTP+JSON)**
1. UCOrchestrator calls `syncTaskToGrpc(task)` → `GrpcBridge.upsertTask()`
2. GrpcBridge sends HTTP POST to `http://localhost:50051/ultimate_coders.TaskService/UpdateTask` (or SubmitTask)
3. gRPC server's `UpdateTask` RPC updates TaskStore, records transition events, broadcasts to WatchTask streams
4. **Gap**: GrpcBridge uses HTTP+JSON, not native gRPC. This works but is a custom protocol, not standard gRPC.

**Path B: Orchestrator → gRPC (via NATS)**
1. Python Orchestrator publishes to `uc.task.update` and `uc.task.event`
2. gRPC server's NATS subscriber receives and applies updates to TaskStore
3. Events are broadcast to WatchTask streams
4. **This is the production path when NATS is available**

**Path C: Orchestrator → gRPC (via LocalWorkerBridge)**
1. Rust gRPC server spawns Python subprocess via LocalWorkerBridge
2. Worker sends `task_update` and `task_event` JSON-RPC notifications on stdout
3. Notification reader applies updates to TaskStore and broadcasts events
4. **This is the path when NATS is unavailable but local worker is available**

**Path D: TUI → gRPC Server**
1. TUI calls TaskService RPCs directly (SubmitTask, PauseTask, ResumeTask)
2. TUI subscribes to WatchTask stream for real-time events
3. **Direct path, no intermediaries**

**Path E: Dashboard → gRPC Server**
1. Dashboard calls TaskService + DashboardService RPCs via gRPC-Web
2. Dashboard subscribes to WatchTask + WatchDashboard streams
3. DashboardService RPCs are NATS passthrough to Python Orchestrator
4. **Direct path for TaskService, NATS-mediated for DashboardService**

---

## 7. State Ownership Analysis

### Who Owns Authoritative Task State?

**UCOrchestrator's `tasks: Map<string, TaskState>`**
- Owns the "live" execution state: `controlState` (running/paused/cancelled), `resumeFromWave`, running subtask results
- Updated in real-time during wave execution
- Persisted to `.uc/tasks/{id}.json` (primary local storage)
- Synced to gRPC TaskStore via `syncTaskToGrpc()` (fire-and-forget, best-effort)

**Rust gRPC server's `TaskStore.tasks: HashMap<String, Task>`**
- Owns the "served" state: what TUI and Dashboard see
- Updated by: (1) direct RPCs (SubmitTask, PauseTask, ResumeTask, UpdateTask), (2) NATS updates, (3) LocalWorkerBridge notifications
- Source of truth for WatchTask event streaming
- In-memory only (no persistence by default, optional PostgreSQL backend)

### Conflict Scenarios

**Scenario 1: TUI pauses a task**
1. TUI calls `PauseTask` RPC → gRPC server sets `task.status = Paused` in TaskStore
2. gRPC server publishes `task_paused` NATS event
3. Python Orchestrator receives NATS event and... **currently does NOT handle it**
4. The orchestrator's `pauseTask()` method is only called directly, not via NATS event
5. **Result**: gRPC TaskStore shows Paused, but Orchestrator's Map still shows running → Orchestrator continues executing waves

**Scenario 2: TUI resumes a task**
1. TUI calls `ResumeTask` RPC → gRPC server sets `task.status = InProgress` in TaskStore
2. gRPC server publishes `task_resumed` NATS event
3. Python Orchestrator receives NATS event and... **currently does NOT handle it**
4. **Result**: gRPC TaskStore shows InProgress, but Orchestrator's Map still shows paused → task never resumes

**Scenario 3: Orchestrator updates task via GrpcBridge**
1. Orchestrator calls `GrpcBridge.upsertTask()` → HTTP POST to `UpdateTask` RPC
2. gRPC server updates TaskStore and broadcasts events
3. **This works correctly** — Orchestrator is the source of truth for execution state

**Scenario 4: Both modify simultaneously**
1. TUI pauses via gRPC → TaskStore shows Paused
2. Orchestrator completes a wave → calls `syncTaskToGrpc()` with `status: "in_progress"` → TaskStore shows InProgress
3. **Result**: Pause is lost — TaskStore flips back to InProgress

### Key Insight: The Orchestrator is the de facto source of truth for execution state, but the gRPC server is the source of truth for what clients see. There is no feedback loop from gRPC control RPCs (Pause/Resume) back to the Orchestrator.

---

## 8. Identified Gaps

### Gap 1: No Cancel RPC
- TUI has a `CANCEL_TASK` reducer action but no gRPC `CancelTask` RPC
- The Rust TaskStore has no `cancel_task()` method
- Cancel is only possible by directly calling `orchestrator.cancelTask()` (via JSON-RPC or omp extension)

### Gap 2: Pause/Resume Not Propagated to Orchestrator
- When TUI/Dashboard calls `PauseTask`/`ResumeTask` via gRPC, the gRPC server updates its own TaskStore
- But the Orchestrator (which actually controls execution) is NOT notified
- The NATS `task_paused`/`task_resumed` events are published by the gRPC server, but the Orchestrator does NOT subscribe to them
- **The Orchestrator only handles pause/resume via direct method calls** (from omp extension or JSON-RPC server)

### Gap 3: Orchestrator GrpcBridge Uses HTTP+JSON, Not Native gRPC
- `GrpcBridge.rpc()` uses `fetch()` with JSON payloads to gRPC-Web-style endpoints
- This works but is non-standard and may have protocol mismatches
- The bridge does NOT use the same proto definitions as the TUI/Dashboard clients

### Gap 4: uc-rpc-server Does Not Forward Events
- The JSONL RPC server handles submit/pause/resume/cancel but does NOT stream events back
- Callers (Python OmpBridge) only get the initial response, not ongoing task events
- Events are only available via the gRPC server's WatchTask stream

### Gap 5: Dual State Without Reconciliation
- Orchestrator's `Map<string, TaskState>` and gRPC server's `TaskStore` can diverge
- `syncTaskToGrpc()` is fire-and-forget with no acknowledgment
- No periodic reconciliation or heartbeat-based state sync
- If gRPC server restarts, its TaskStore is empty (in-memory only) while Orchestrator may have persisted tasks on disk

### Gap 6: Dashboard Pause/Resume Goes Through TaskService, Not Orchestrator
- Dashboard's `pauseTask`/`resumeTask` in `useGrpcWeb.ts` call `TaskService.PauseTask`/`ResumeTask`
- These only update the gRPC TaskStore, not the Orchestrator
- For the Dashboard to actually pause/resume execution, it would need to go through the Orchestrator (via NATS or direct call)

### Gap 7: No Orchestrator Health/Status Endpoint for TUI
- TUI connects directly to gRPC server but has no way to know if an Orchestrator is actually running
- If gRPC server is up but Orchestrator is down, TUI can submit tasks that will sit in Planning status forever
- The `Health` RPC reports `local_worker` status but not Orchestrator availability

---

## 9. Communication Protocol Summary

| From | To | Protocol | Methods |
|---|---|---|---|
| TUI | gRPC Server | Native gRPC (`@grpc/grpc-js`) | SubmitTask, GetTask, ListTasks, WatchTask, PauseTask, ResumeTask |
| Dashboard | gRPC Server | gRPC-Web (`@connectrpc/connect-web`) | Same as TUI + DashboardService RPCs |
| Dashboard | gRPC Server | SSE (fallback) | `/dashboard/api/stream` |
| Orchestrator | gRPC Server | HTTP+JSON (GrpcBridge) | SubmitTask, UpdateTask, PauseTask, ResumeTask, ReadMemory, WriteMemory, SearchMemory, Search |
| Orchestrator | gRPC Server | NATS (production) | Publishes to `uc.task.update`, `uc.task.event` |
| gRPC Server | Orchestrator | NATS | Publishes to `uc.task.submit`, `uc.subtask.execute`, `uc.task.event` (pause/resume) |
| gRPC Server | Local Worker | JSON-RPC stdin/stdout | submit_task, task_update, task_event notifications |
| Python OmpBridge | uc-rpc-server | JSONL stdin/stdout | submit_task, pause_task, resume_task, cancel_task, show_status |

---

## Caveats / Not Found

- **No `CancelTask` RPC** exists in the proto or server implementation. Cancel is only available via direct Orchestrator method call.
- **No Orchestrator→NATS subscription** for `task_paused`/`task_resumed` events was found. The Orchestrator does not react to gRPC-initiated pause/resume.
- **No reconciliation mechanism** between Orchestrator's local Map and gRPC TaskStore was found.
- The `GrpcBridge` HTTP+JSON protocol is not documented as a formal API — it's an implementation detail.
- The uc-rpc-server's event forwarding gap is by design (it's a simple request-response bridge), but it means callers cannot observe task progress without connecting to the gRPC WatchTask stream separately.
