# Research: UC Orchestrator Extension Interruption Causes

- **Query**: How UC Orchestrator extension handles session events, gRPC bridge failures, state management
- **Scope**: Internal
- **Date**: 2026-06-27

## Findings

### Files Found

| File Path | Description |
|---|---|
| `packages/uc-orchestrator/src/extension.ts` | Extension entry point: session_start/session_shutdown hooks, command registration |
| `packages/uc-orchestrator/src/orchestrator/grpc-bridge.ts` | gRPC-Web bridge to Rust server: single transport, no reconnect, all errors swallowed |
| `packages/uc-orchestrator/src/orchestrator/orchestrator.ts` | Core orchestrator: task lifecycle, circuit breaker, abort controllers, persistence |
| `packages/uc-orchestrator/src/orchestrator/events.ts` | Typed event emitter with clear() on shutdown |
| `packages/uc-orchestrator/src/orchestrator/control-signal-subscriber.ts` | NATS control subscription with polling fallback |
| `packages/uc-orchestrator/src/orchestrator/task-store.ts` | Local JSON persistence for task state |

### Code Patterns

#### 1. Extension Session Hooks

From `extension.ts`:
- `pi.on("session_start", ...)`: Creates `FooterStatusRenderer`, wires 12 orchestrator event types to UI updates. Sets initial status "UC: ready"
- `pi.on("session_shutdown", ...)`: Calls `orchestrator.events.clear()` to remove all event handlers

**Problem**: The `session_shutdown` hook only clears the event emitter's handlers. It does NOT:
- Stop the `ControlSignalSubscriber` (NATS connection + polling timer)
- Cancel running tasks' abort controllers
- Close the `GrpcBridge` transport
- Clear `progressState` map
- Stop the `TaskStore` polling

The `UCOrchestrator` class has no explicit `destroy()` or `cleanup()` method. On session restart (new session in same OMP process), the old orchestrator's internal state persists -- `tasks` Map, `abortControllers` Map, `runningCount`, `circuitBreaker` state all remain from the previous session.

#### 2. GrpcBridge -- No Reconnect

The `GrpcBridge` is the critical vulnerability point:

```typescript
constructor(config?: Partial<BridgeConfig>) {
    this.config = { serverUrl: "http://localhost:50051", timeoutMs: 10_000, ...config };
    this.transport = createGrpcWebTransport({ baseUrl: this.config.serverUrl });
    this.engineClient = createClient(EngineService, this.transport);
    this.taskClient = createClient(TaskService, this.transport);
    this.dashboardClient = createClient(DashboardService, this.transport);
}
```

- **Single transport**: Created once at construction. If the gRPC server dies and restarts, the transport's underlying HTTP/2 connection is stale
- **All errors swallowed**: Every method catches all exceptions and returns `null`, `false`, `[]`, or `{ status: "unavailable" }`. No error is propagated to the caller. The `connected` flag is set to `true` on successful `health()` calls but never set back to `false` on failures (only `health()` sets `connected = true`; other methods set it implicitly)
- **No timeout enforcement**: The `timeoutMs: 10_000` config is defined but never passed to `createGrpcWebTransport`. The connectrpc transport has its own default timeouts which may differ
- **Fire-and-forget gRPC sync**: `syncTaskToGrpc()` calls `bridge.upsertTask(...)` and `.catch(() => {})`. If the server is unreachable, the task state is silently lost on the server side

#### 3. Circuit Breaker and Abort Patterns

`orchestrator.ts` implements:
- `CircuitBreaker`: Tracks consecutive failures. When threshold reached, `canExecute()` returns false. Reset between waves and on new task submission
- `AbortController per task`: Stored in `abortControllers` Map. On cancel, abort is signaled. On resume, a new AbortController is created
- **Subtask timeout**: Default `subtaskTimeoutMs: 600_000` (10 min). Uses `AbortSignal.timeout()` via `runSubprocess` signal parameter
- **Worker availability check**: Before each wave, checks `bridge.listWorkers()`. If no workers available, retries once after 5s, then fails the task

#### 4. Control Signal Subscriber

`control-signal-subscriber.ts`:
- **NATS-first**: Connects to NATS with 2s timeout. If unavailable, falls back to polling
- **Polling**: Creates a new `GrpcBridge` instance every 2s interval, calls `getTask()` for each active task
- **No cleanup on session shutdown**: The `stop()` method exists but is never called from `extension.ts`'s `session_shutdown` hook. The `start()` method is called in `orchestrator.restore()` but there's no corresponding `stop()` call
- **Dedup**: `seenMessageIds` Map with 5-minute TTL, max 10K entries. Prevents double-processing of NATS events
- **NATS subscription**: `natsConn.subscribe("uc.task.event")` -- subscribes to ALL task events, not just control events. The handler filters by type but the subscription receives the full stream

#### 5. Event System Leakage

`OrchestratorEventEmitter` (events.ts):
- `on()`: Returns unsubscribe function. But `extension.ts` never calls these unsubscribe functions -- it relies on `clear()` at session_shutdown
- `clear()`: Removes all handlers. But if a session ends and a new one starts, the `extension.ts` code re-registers all handlers. The old `orchestrator` instance (from `restore()`) still exists with stale task state
- **No weak references**: The event emitter uses `Set<RpcSubagentLifecycleListener>` etc. -- strong references that prevent GC of listener closures

### Caveats / Not Found

- No explicit memory pressure monitoring or GC-triggering in the orchestrator
- The `TaskStore` (local JSON persistence) has no size limit -- tasks accumulate indefinitely
- The `GrpcBridge.connected` flag can be stale: set to `true` on successful health check, but never explicitly set to `false` on connection errors (other methods just catch and return)
- There's no mechanism to detect that the gRPC server has restarted (run-omp.sh health_monitor restarts it) and refresh the transport
