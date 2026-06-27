# OMP Tools Spec

> Contracts for LLM-callable tools registered by the UC Orchestrator OMP extension.

---

## 1. Scope / Trigger

This spec covers the OMP tool layer in `packages/uc-orchestrator/src/orchestrator/`:
- Tool registration via `pi.registerTool()`
- GrpcBridge method signatures and RPC payload contracts
- Zod schema definitions for tool parameters
- Error handling and response formatting conventions

Trigger: any change to tool schemas, bridge methods, or new tool registration.

---

## 2. Tool Registry

| Tool | File | Bridge Methods | Description |
|------|------|----------------|-------------|
| `uc_memory` | memory-bridge.ts | readMemory, writeMemory, searchMemory, deleteMemory | Layered memory CRUD |
| `uc_search` | memory-bridge.ts | searchCode | Hybrid code search |
| `uc_task` | task-bridge.ts | submitTask, cancelTask, pauseTask, resumeTask, getTask, listTasks | Task lifecycle |
| `uc_index` | index-bridge.ts | indexRepo, getIndexState, removeIndex, listRepos | Index management |
| `uc_file` | file-bridge.ts | listDir, getFile | File browsing |
| `uc_worker` | worker-bridge.ts | listWorkers | Worker status awareness |

Registration pattern: each file exports `register*(pi, bridge)` called from `extension.ts`.

---

## 3. Tool Schemas

### uc_memory

```typescript
{
  action: "read" | "write" | "search" | "delete",
  scope: string,           // short_term|long_term|metadata|task|project|global
  key: string,
  content?: string,        // required for write
  content_type?: string,   // text|structured|code|diff|reference (default: text)
  importance?: number,     // 0-1, >= 0.7 writes to long-term
  tags?: string[],         // categorization labels
}
```

### uc_search

```typescript
{
  query: string,
  modes?: string[],         // text|semantic|ast|hybrid (default: hybrid)
  max_results?: number,     // default: 5
  repo_ids?: string[],      // filter to specific repos
  languages?: string[],     // filter by language (e.g. ['typescript','rust'])
  path_patterns?: string[], // glob patterns (e.g. ['src/**/*.ts'])
}
```

### uc_task

```typescript
{
  action: "submit" | "cancel" | "pause" | "resume" | "status",
  task_id?: string,         // required for cancel/pause/resume/status
  description?: string,     // required for submit
  subtask_id?: string,      // optional, for cancel of specific subtask
}
```

### uc_index

```typescript
{
  action: "index_repo" | "list_repos" | "get_state" | "remove_index",
  repo_id?: string,         // required for index_repo/get_state/remove_index
  local_path?: string,      // required for index_repo
  languages?: string[],     // optional for index_repo
}
```

### uc_file

```typescript
{
  action: "list_dir" | "get_file",
  path: string,
  repo_id?: string,         // optional scoping
}
```

### uc_worker

```typescript
{
  action: "list" | "status",
  worker_id?: string,       // required for status; prefix matching supported
}
```

---

## 4. GrpcBridge Method Contracts

### TaskService RPCs

| Method | Payload | Response | Error |
|--------|---------|----------|-------|
| SubmitTask | `{description, project_id}` | `{success, id, status, subtasks[]}` | catch → null |
| CancelTask | `{task_id, subtask_id?}` | `{success}` | catch → false |
| PauseTask | `{task_id}` | `{success}` | catch → false |
| ResumeTask | `{task_id}` | `{success}` | catch → false |
| GetTask | `{task_id}` | `{available, task}` | catch → null |
| ListTasks | `{}` | `{available, tasks[]}` | catch → [] |

### EngineService RPCs

| Method | Payload | Response | Error |
|--------|---------|----------|-------|
| ReadMemory | `{key_scope, key, task_id, project_id}` | `{entry}` | catch → null |

### DashboardService RPCs

| Method | Payload | Response | Error |
|--------|---------|----------|-------|
| ListWorkers | `{}` | `{available, workers[{id, capabilities, current_load, max_capacity, load_percent, last_heartbeat, heartbeat_age_seconds, heartbeat_stale, is_available}], total, available_count}` | catch → Health RPC fallback → `{available: true, workers:[{id:"local_worker", max_capacity:-1, degraded:true}], total:1}` or `{available: false, workers:[]}` |

**Degraded mode**: When `ListWorkers` RPC fails (NATS unavailable), `listWorkers()` falls back to the `Health` RPC's `local_worker` component. The synthetic worker has `maxCapacity: -1` and `WorkerListResult.degraded: true` to signal approximate data. Display code checks `maxCapacity < 0` to render "unknown (degraded mode)" instead of misleading "0/0 (0%)".
| WriteMemory | `{key_scope, key, content, content_type, source_agent, task_id, project_id, importance?, tags?}` | (empty) | catch → false |
| SearchMemory | `{query, scope_type, project_id, max_results}` | `{results[]}` | catch → [] |
| DeleteMemory | `{key_scope, key, task_id, project_id}` | `{success}` | catch → false |
| BatchWriteMemory | `{entries[{key_scope, key, content, content_type, source_agent}]}` | `{count}` | catch → 0 |
| Search | `{query, modes, max_results, repo_ids?, languages?, path_patterns?}` | `{items[]}` | catch → [] |
| IndexRepo | `{repo_id, local_path, languages}` | `{success}` | catch → false |
| GetIndexState | `{repo_id}` | `{available, status, indexed_files, last_indexed}` | catch → null |
| RemoveIndex | `{repo_id}` | `{success}` | catch → false |
| ListRepos | `{}` | `{repos[]}` | catch → [] |
| ListDir | `{path, repo_id?}` | `{entries[]}` | catch → [] |
| GetFile | `{path, repo_id?}` | `{content}` | catch → null |

---

## 5. Conventions

### Error handling
- Bridge methods: `try/catch` returning `null/false/[]` (never throw)
- Tool execute: `try/catch` returning `{ content: [...], isError: true }`
- Empty results: return `{ content: [...], useless: true }` (tells LLM the result is not useful)

### Response format
- All tools return `{ content: [{ type: "text", text: string }] }`
- Successful operations: human-readable summary
- Lists: one item per line, truncated (IDs → 8 chars, descriptions → 60 chars)
- File content: truncated at 8000 chars with `... (truncated)` suffix

### Scope mapping
OMP tools use `short_term/long_term/metadata`, gRPC uses `task/project/global`.
Mapping in `memory-bridge.ts`:
- `short_term` → `task`
- `long_term` → `global`
- `metadata` → `project`

### RPC service routing
`GrpcBridge.resolveService(method)` maps method names to gRPC service paths:
- `TaskService`: SubmitTask, GetTask, ListTasks, WatchTask, PauseTask, ResumeTask, CancelTask, UpdateTask
- `EngineService`: Search, IndexRepo, GetIndexState, RemoveIndex, ReadMemory, WriteMemory, DeleteMemory, SearchMemory, Health, BatchWriteMemory, ListRepos, ListDir, GetFile
- `DashboardService`: ListWorkers, GetSchedulerStatus, GetDashboardData

### Worker availability check in Orchestrator
Before each wave execution, `UCOrchestrator.checkWorkerAvailability()` calls `bridge.listWorkers()`. If no workers are available (`availableCount === 0`), the task fails fast with error "No workers available — all workers offline or overloaded" instead of letting subtasks time out.

### Zod schema pattern
```typescript
const schema = pi.zod.object({ ... });
pi.registerTool({
  name: "uc_xxx",
  parameters: schema as never,  // dodge TS2589 deep instantiation
  async execute(_id, params: unknown, ...) {
    const p = params as { ... };  // manual cast
  },
});
```

---

## 6. GrpcBridge Reconnect & Session Lifecycle

### Reconnect mechanism

GrpcBridge detects stale transport and auto-reconnects:

```
connection error → isConnectionError() → tryReconnect() → reconnect() → retry once → fallback
```

**Key methods:**

| Method | Purpose |
|--------|---------|
| `reconnect()` | Recreates transport + all 3 service clients, sets `connected=false` |
| `tryReconnect()` | Reentrant-guarded reconnect (max 1 attempt), validates via `health()`, sets `connected=true` on success |
| `withReconnect<T>(fn, fallback)` | Wraps RPC call: try → catch connection error → reconnect+retry → return fallback |
| `isConnectionError(err)` | Detects ECONNREFUSED, ECONNRESET, fetch failed, HTTP2 GoAway, transport errors |
| `checkRestartMarker()` | Reads `/tmp/uc-grpc-restart-marker` (unix timestamp); triggers reconnect if timestamp increased |
| `close()` | Sets `connected=false`, no transport teardown (connectrpc manages its own) |

**Restart marker protocol:** `run-omp.sh` writes `date +%s` to `/tmp/uc-grpc-restart-marker` on gRPC server start and on health_monitor restart. `GrpcBridge.health()` calls `checkRestartMarker()` to detect server restarts.

**Connection error patterns** (isConnectionError):
- ECONNREFUSED, ECONNRESET, EPIPE, ETIMEDOUT, ENOTFOUND
- "failed to fetch", "fetch failed", "network", "http2", "goaway", "stream reset", "transport", "connect-reset"

### Session lifecycle

**session_start** (extension.ts):
1. `orchestrator.events.clear()` — remove stale handlers from previous session
2. Register 12 event handlers (orchestrator events → UI updates)
3. Set status "UC: ready"

**session_shutdown** (extension.ts):
1. `orchestrator.destroy()` — full cleanup:
   - Stop `ControlSignalSubscriber` (NATS connection + polling timer)
   - Abort all `AbortController`s for running tasks
   - Clear `tasks` Map
   - Reset `circuitBreaker`
   - Call `bridge.close()` (marks disconnected)
   - Clear event handlers
2. `progressState.clear()` — remove all widget state

**Task eviction** (`evictCompletedTasks(maxTasks=100)`):
- Called after each wave completes
- When tasks Map exceeds maxTasks, evicts oldest completed/failed/cancelled tasks
- Sorts by `completedAt` timestamp (oldest first)
- After in-memory eviction, calls `store.removeStale(remainingIds)` to clean up disk files

### Connection state change notification

`BridgeConfig.onConnectionChange?: (connected: boolean) => void` — optional callback fired on connection state transitions. Orchestrator wires this to:
1. `events.emit("connection_state", { connected })` — for UI updates
2. **Bulk resync on reconnect**: when `connected` transitions `false → true`, calls `resyncAllTasksToGrpc()` to push all local tasks to the (now-empty) server TaskStore. Fire-and-forget with failure logging.

Extension.ts handles the `connection_state` event to update footer status:
- `connected=true` → "UC: connected"
- `connected=false` → "UC: disconnected"

The callback can be set post-construction via `bridge.setOnConnectionChange(cb)` — needed when the bridge is created externally (e.g., in extension.ts).

### Worker availability in degraded mode

When `bridge.listWorkers()` returns `{ available: false }` AND `!bridge.isConnected()`, the orchestrator assumes local workers are available (degraded mode) and returns `true`. This prevents false task failures during gRPC outages — actual subtask execution uses local `runSubprocess` which doesn't need gRPC workers.

### Subprocess abort propagation

Decompose and review subprocesses use `abortWithTimeout(signal, timeoutMs)` instead of `AbortSignal.timeout()` alone. This combines the task's `AbortController.signal` with a timeout — whichever fires first aborts the subprocess. When a task is cancelled, its decompose/review subprocesses are immediately aborted instead of running until timeout.

### uc_task tool local fallback

When `uc_task submit` fails with `server_unavailable` and the orchestrator is available, it falls back to `orchestrator.submitTask()` which works fully offline (local decomposition + execution). This ensures task submission works even when gRPC is down.

### ControlSignalSubscriber architecture

- Shares the main `GrpcBridge` instance (no separate bridge for polling)
- NATS-first with auto-reconnect: 3 attempts, exponential backoff (1s/2s/4s)
- Falls back to polling via shared bridge if NATS unavailable after all retries
- Polling interval: 2s (configurable via `pollIntervalMs`)

### Heartbeat timeout

gRPC server heartbeat timeout: **120s** (was 600s). Worker crash detected in 2 minutes instead of 10. Subtask execution timeout remains 600s (10 min).

---

## 7. Common Mistakes

### Adding bridge params but not forwarding to RPC payload
**Symptom**: Schema accepts new parameters but they're silently dropped.
**Fix**: When adding schema params, always update the bridge method signature AND the RPC payload construction.
**Example**: `importance`/`tags` were in schema but `writeMemory` didn't include them in the JSON payload sent to gRPC.

### Registering tool with unused orchestrator parameter
**Symptom**: `registerTaskTools(pi, bridge, orchestrator)` where `orchestrator` is never used.
**Fix**: Only pass what's needed. If all operations go through bridge, don't pass orchestrator.

### Forgetting to check restart marker on connection failure
**Symptom**: GrpcBridge says `connected=true` but all calls fail after gRPC server restart.
**Fix**: Always call `checkRestartMarker()` in `health()` — the restart marker is the signal that the server was restarted by `run-omp.sh` health_monitor.

### Not calling events.clear() before session_start handler registration
**Symptom**: Event handlers accumulate (12 new per session_start) — growing handler count, slower dispatch.
**Fix**: Always call `orchestrator.events.clear()` at the start of the session_start handler, before registering new handlers.

### Leaking timers/subscribers across sessions
**Symptom**: ControlSignalSubscriber polling timer continues running after session ends, creating new GrpcBridge instances every 2s.
**Fix**: `orchestrator.destroy()` calls `subscriber.stop()`. Always call destroy() in session_shutdown, never just events.clear().

---

## 8. Out of Scope (Phase 3 — not implemented)

| Tool | Reason |
|------|--------|
| `uc_schedule` | Phase 2, needs Python RPC bridge for SchedulerService |
| `uc_checkpoint` | Auto wave-boundary checkpoint sufficient; explicit low-frequency |
| `uc_circuit_breaker` | Monitoring only, agent rarely calls |
| `uc_rate_limiter` | Monitoring only |
| Edit intent / conflict tools | Worker-internal coordination, not LLM-driven |
| Sandbox execution tool | Security risk, not exposed |
| WatchTask streaming | OMP tools are request-response, not streaming |
