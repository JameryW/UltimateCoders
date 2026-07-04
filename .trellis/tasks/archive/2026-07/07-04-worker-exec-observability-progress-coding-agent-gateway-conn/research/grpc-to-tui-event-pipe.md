# Research: gRPC-to-TUI Event Pipe (subtask_progress streaming)

- **Query**: How to stream worker execution events (subtask_progress with phase/percent/step_agent) from Rust gRPC server to OMP TUI (uc-orchestrator TS extension)
- **Scope**: internal
- **Date**: 2026-07-04

## Findings

### Files Found

| File Path | Description |
|---|---|
| `crates/uc-grpc/proto/engine.proto` | Proto definitions — 3 server-streaming RPCs exist: `WatchTask`, `WatchDashboard`, `SearchStream` |
| `crates/uc-grpc/src/server.rs` | gRPC server: broadcast channel (cap 256), `watch_task` impl (lines 3110-3213), NATS subscriber, `nats_event_to_agent_event` (line 2424) |
| `crates/uc-grpc/src/dashboard_service.rs` | `WatchDashboard` impl — subscribes to NATS `uc.task.event` + `uc.dashboard.snapshot` directly (lines 199-373) |
| `crates/uc-engine/src/events.rs` | `AgentEventType` enum — NO `SubtaskProgress` variant exists (line 35) |
| `crates/uc-grpc/src/conversions.rs` | `From<AgentEventType> for TaskEventProto` (line 917) — no subtask_progress case |
| `packages/uc-orchestrator/src/orchestrator/grpc-bridge.ts` | TUI gRPC client — does NOT import `WatchTaskRequestSchema`, makes only unary RPC calls, no streaming subscription |
| `packages/uc-orchestrator/src/orchestrator/events.ts` | `OrchestratorEvents` interface — NO `subtask_progress` event type (line 14) |
| `packages/uc-orchestrator/src/orchestrator/control-signal-subscriber.ts` | TUI already has NATS access — subscribes to `uc.task.event` (line 122), but only handles `task_paused/resumed/cancelled` |
| `packages/uc-orchestrator/src/grpc/engine_pb.ts` | Generated proto TS — `watchTask` is `server_streaming` (line 3144), available but unused by GrpcBridge |
| `dashboard/src/hooks/useGrpcWeb.ts` | Dashboard's gRPC-Web client — uses `createClient(TaskService)` + `client.watchTask()` with async-for-await stream (lines 210-235) |
| `python/ultimate_coders/agent/worker.py` | Worker publishes `subtask_progress` events to NATS with `data={phase, percent, worker_id, step_agent}` (lines 657-663, 1078-1103) |
| `python/ultimate_coders/nats_worker.py` | `NatsPublisher.publish_event` sends to `uc.task.event` (lines 216-229) |

### Existing Streaming Infrastructure

**1. Broadcast channel (Rust gRPC server) — `crates/uc-grpc/src/server.rs:1326`**

```rust
// GrpcServerInner struct (line 1314-1327)
struct GrpcServerInner<E> {
    event_tx: broadcast::Sender<TaskEvent>,  // capacity 256
}
```
- Created at `server.rs:1350` (`broadcast::channel(256)`)
- **Multi-subscriber**: `tokio::sync::broadcast` supports N receivers. Each `event_tx.subscribe()` creates an independent receiver. Already used by `WatchTask` streams (each TUI/dashboard client gets its own receiver).
- All event sources publish to `event_tx`:
  - NATS `uc.task.event` subscriber (line 2053: `let _ = event_tx.send(proto_event);`)
  - NATS `uc.task.update` subscriber (line 2012: `for event in new_events { let _ = event_tx.send(event); }`)
  - Local task operations (pause/resume/cancel — lines 3232, 3269, 3313)

**2. WatchTask server-streaming RPC — `server.rs:3110-3213`**

```rust
type WatchTaskStream = Pin<Box<dyn Stream<Item = Result<TaskEvent, Status>> + Send>>;

async fn watch_task(&self, request: Request<WatchTaskRequest>) -> Result<Response<Self::WatchTaskStream>, Status> {
    let event_rx = self.inner.event_tx.subscribe();  // subscribe before replay
    // Phase 1: replay existing events from TaskStore (skipped if taskId empty)
    // Phase 2: live broadcast events via rx.recv().await
}
```
- Empty `task_id` = "watch all" (skips replay, only live events) — line 3137
- Already handles `sync_required` on broadcast lag (line 3192)
- Streams `TaskEvent` proto: `{timestamp, type, task_id, subtask_id, data: map<string,string>}`

**3. WatchDashboard server-streaming RPC — `dashboard_service.rs:199-373`**
- When `messaging` feature ON: subscribes to NATS `uc.task.event` + `uc.dashboard.snapshot` directly (NOT the broadcast channel), pushes `DashboardSnapshot` with `recent_task_events`
- When `messaging` feature OFF: subscribes to the broadcast channel (`event_sender().subscribe()`, line 339), pushes `DashboardSnapshot` with `recent_task_events` from broadcast

### The subtask_progress Gap

**Python worker publishes it** (`worker.py:657-663`):
```python
async def _progress(phase: str, percent: int, **extra: Any) -> None:
    await self._publish_event(
        "subtask_progress",
        task_id=subtask.parent_id,
        subtask_id=subtask.id,
        data={"phase": phase, "percent": percent, "worker_id": self.worker_id, **extra},
    )
```
Called at: `preparing`(10), `executing`(50), `validating`(80), and per-workflow-step with `step_agent` (lines 1078-1103).

**Rust gRPC server DROPS it** — `nats_event_to_agent_event` (`server.rs:2424-2604`) has no `"subtask_progress"` match arm. The `_ =>` catch-all (line 2597) logs `"Ignoring unrecognized NATS event type"` and returns `None`. So:
- The event is NOT recorded in the EventStore
- The event is NOT broadcast on `event_tx`
- WatchTask streams never see it
- Dashboard SSE never sees it (dashboard gets events from NATS directly in messaging mode, but the Rust `WatchDashboard` NATS path converts to `DashboardSnapshot` which has no progress field)

**No `SubtaskProgress` variant in `AgentEventType`** (`events.rs:35-123`). The enum has: TaskCreated, SubtaskAssigned, SubtaskStarted, ToolInvoked, ToolResult, FileModified, EditIntent, SubtaskCompleted, SubtaskFailed, CheckpointCreated, TaskCompleted, TaskFailed, TaskPaused, TaskResumed, TaskCancelled, TaskUpdated.

**Proto `TaskEvent` has no dedicated progress fields** but IS flexible: `data: map<string, string>` (proto line 343) can carry `phase`, `percent`, `step_agent` as string values. The `type` field (proto line 340) is a free-form string — already carries `"subtask_started"`, `"tool_call"`, etc. Adding `"subtask_progress"` as a type string requires NO proto change.

### TUI Connection Infrastructure

**GrpcBridge** (`grpc-bridge.ts`) uses `@connectrpc/connect-web` — same library as dashboard. It creates a `createGrpcWebTransport({baseUrl})` (line 135) and `createClient(TaskService, transport)` (line 127). The `taskClient` is already available but only calls unary RPCs (submitTask, getTask, listTasks, updateTask, pause/resume/cancel).

**The generated proto TS** (`engine_pb.ts:3143`) declares `watchTask` as `server_streaming`. The dashboard already uses this exact pattern (`useGrpcWeb.ts:216`): `const stream = client.watchTask(req, {signal}); for await (const event of stream) {...}`. The TUI's `@connectrpc/connect` supports server-streaming via async iteration — no additional dependency needed.

**TUI has NATS access** via `ControlSignalSubscriber` (`control-signal-subscriber.ts:122`), which subscribes to `uc.task.event`. But it only handles `task_paused/resumed/cancelled` (line 175 filter). It uses the `nats` npm package, connects to `nats://localhost:4222` (env `UC_NATS_URL`).

### Ranked Minimal Approaches

**Approach A (RECOMMENDED — minimal change): Reuse existing WatchTask gRPC stream from TUI**

Changes needed:
1. `crates/uc-grpc/src/server.rs` `nats_event_to_agent_event()` — add `"subtask_progress"` match arm that converts to a `TaskEvent` proto directly (bypass `AgentEventType` since there's no variant for it — construct `TaskEvent` inline like the NATS event already provides, or add a generic passthrough). The simplest path: in the NATS subscriber (`server.rs:2046-2054`), when `nats_event_to_agent_event` returns `None` but `event.r#type == "subtask_progress"`, construct a `TaskEvent` proto directly from the `NatsTaskEvent` fields and broadcast it on `event_tx`.
2. `packages/uc-orchestrator/src/orchestrator/grpc-bridge.ts` — add a `watchTask()` method that opens the server-streaming RPC and emits events via a callback. Pattern is identical to `dashboard/src/hooks/useGrpcWeb.ts:210-235`.
3. `packages/uc-orchestrator/src/orchestrator/events.ts` — add `subtask_progress: {taskId, subtaskId, phase, percent, stepAgent?}` to `OrchestratorEvents`.
4. TUI UI component — render the progress (phase/percent) in the subtask view.

Why minimal:
- NO proto change (TaskEvent.data map already carries arbitrary fields)
- NO new RPC (WatchTask already exists and is streaming)
- NO new dependency (TUI already has `@connectrpc/connect-web`)
- Broadcast channel already multiplexes to N subscribers (dashboard + TUI coexist)
- Dashboard already proves the pattern works end-to-end

**Approach B: Add a new dedicated event-stream RPC**

Would require: new proto RPC (e.g. `WatchEvents`), new server impl, new client method. More work, no benefit over Approach A since WatchTask already streams `TaskEvent` and the data map is flexible enough. Only justified if WatchTask's semantics (task-scoped) conflict with progress events — but WatchTask with empty `task_id` already means "watch all".

**Approach C: TUI subscribes to NATS directly**

The TUI already has NATS access via `ControlSignalSubscriber`. Could add `subtask_progress` to its event filter (line 175). However:
- Bypasses the gRPC server's dedup, EventStore persistence, and broadcast fan-out
- Creates a second NATS consumer (the gRPC server is the first) — duplicates message processing
- `ControlSignalSubscriber` is designed for control signals (pause/resume/cancel), not progress display
- Would not benefit the dashboard (which uses gRPC-Web, not NATS)
- Requires NATS to be available (gRPC path degrades gracefully; NATS path does not)

Ranked LAST — breaks the single-pipe architecture, benefits only TUI, and duplicates work the gRPC server already does.

### Code Patterns

**Dashboard consuming WatchTask stream** (`dashboard/src/hooks/useGrpcWeb.ts:210-235`):
```typescript
const transport = getTransport();
const client = createClient(TaskService, transport);
const req = create(WatchTaskRequestSchema, { taskId: "" }); // empty = watch all
const stream = client.watchTask(req, { signal: ac.signal });
for await (const event of stream) {
  if (ac.signal.aborted) break;
  if (event.type === "sync_required") { /* re-sync */ continue; }
  const dashboardEvent = grpcEventToDashboardEvent(event);
  optsRef.current.onTaskEvent?.(dashboardEvent);
}
```
This exact pattern can be copied into GrpcBridge with minimal adaptation.

**Broadcast channel fan-out** (`server.rs:3110-3213`):
- `event_tx.subscribe()` (line 3125) creates a new receiver
- Multiple WatchTask streams each call `subscribe()` independently
- Already supports dashboard + TUI + any other consumer simultaneously
- Lag handling: `sync_required` event tells client to re-sync (line 3192)

## Caveats / Not Found

- `subtask_progress` events are currently SILENTLY DROPPED by the Rust gRPC server (confirmed: no match arm in `nats_event_to_agent_event`, catch-all returns `None` at line 2597). This means even the dashboard's SSE stream does NOT currently show subtask_progress — the dashboard's `WatchDashboard` NATS path converts events to `DashboardSnapshot` via `event_to_dashboard_snapshot` (line 844), which only populates `recent_task_events`, and the Rust `WatchDashboard` only subscribes to `uc.task.event` NATS directly when `messaging` is enabled. So progress events ARE on NATS but the Rust server's NATS subscriber drops them before they reach the broadcast channel.
- The `TaskEvent.data` field is `map<string, string>` — `percent` (a number in Python) would arrive as a string. The dashboard's `grpcEventToDashboardEvent` conversion would need to `parseInt`/`parseFloat` it. This is an existing pattern (other events already stringify numbers).
- Did not verify whether `@connectrpc/connect-web` in a Bun/Node environment (OMP TUI runs in Bun) supports server-streaming identically to the browser. The dashboard runs in a browser (Vite). The TUI runs via `run-omp.sh` which uses Bun. The `@connectrpc/connect` library is runtime-agnostic (uses fetch/streams), so it should work, but this was not verified at runtime.
