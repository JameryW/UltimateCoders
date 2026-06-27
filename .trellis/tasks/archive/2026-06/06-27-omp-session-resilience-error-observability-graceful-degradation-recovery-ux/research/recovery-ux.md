# Research: Recovery UX

- **Query**: After a gRPC server restart + reconnect, does the orchestrator re-sync task state, re-submit active tasks, and handle in-progress tasks correctly?
- **Scope**: internal
- **Date**: 2026-06-27

## Findings

### Files Found

| File Path | Description |
|---|---|
| `packages/uc-orchestrator/src/orchestrator/grpc-bridge.ts` | Reconnect logic + restart marker check |
| `packages/uc-orchestrator/src/orchestrator/orchestrator.ts` | Task state management + sync |
| `packages/uc-orchestrator/src/orchestrator/task-store.ts` | Local JSON persistence |
| `packages/uc-orchestrator/src/orchestrator/control-signal-subscriber.ts` | NATS/polling fallback |

### Reconnect Mechanism

The gRPC bridge has a reconnect mechanism (`grpc-bridge.ts:178-195`):
1. `tryReconnect()` detects connection errors, recreates transport, verifies with health check
2. `checkRestartMarker()` (line 198-209) reads `/tmp/uc-grpc-restart-marker` to detect server restarts
3. `withReconnect()` (line 216-231) wraps RPC calls with automatic retry-on-connection-error
4. The `reconnecting` flag (line 112) prevents concurrent reconnect attempts

The orchestrator wires connection state events (`orchestrator.ts:137-139`):
```typescript
this.bridge.setOnConnectionChange((connected: boolean) => {
  this.events.emit("connection_state", { connected });
});
```

Extension.ts renders connection state in the footer (`extension.ts:158-162`).

### Recovery Analysis

#### 1. Does the orchestrator re-sync its task state with the (now-empty) server TaskStore?

**No.** After a gRPC server restart:
- The server's in-memory TaskStore is empty (it's `HashMap<String, Task>` in Rust, not persisted)
- The orchestrator's local `this.tasks` Map still has all tasks
- `syncTaskToGrpc()` (line 1250-1255) pushes local state to gRPC, but it's fire-and-forget
- There is NO periodic re-sync mechanism that pushes all local tasks to gRPC
- After reconnect, the bridge's `connected` flag is set to `true`, but no bulk sync is triggered

**What happens**: The gRPC server's TaskStore is empty after restart. The orchestrator has tasks locally but they are invisible to the gRPC server. Individual `syncTaskToGrpc()` calls happen only when tasks change state (wave completion, cancel, etc.). Tasks that are already in a stable state (e.g., completed before the restart) are never re-synced.

**The restart marker** (`checkRestartMarker()`, line 198-209) only triggers `this.reconnect()` (transport recreation). It does NOT trigger a bulk sync of local tasks to the server.

#### 2. Does it re-submit active tasks to the server?

**No.** After reconnect:
- Tasks that were `in_progress` in the orchestrator's Map remain in memory
- Their execution continues (executeWaves is still running in the async call stack)
- But the gRPC server has no record of these tasks
- `syncTaskToGrpc()` calls during execution will push updates, but the server may not have the base task record
- `upsertTask()` (grpc-bridge.ts:313-348) calls `getTask()` first, then either `updateTask()` or `submitTask()`. If the task doesn't exist on the server, it will be re-created via `submitTask()`.
- **Partial recovery**: The next `syncTaskToGrpc()` call for an active task will re-create it on the server via `upsertTask()`. But this only happens when task state changes (wave completion, etc.), not proactively on reconnect.

#### 3. What happens to tasks that were in-progress when the server went down?

**Analysis by phase**:

**Decomposition phase** (task.status = "planning"):
- `decompose()` uses `runSubprocess` (OMP local agent) -- not gRPC dependent
- If gRPC goes down during decomposition, the decompose call continues unaffected
- After decomposition, `syncTaskToGrpc()` will fire-and-forget, which will fail silently
- The task proceeds to execution

**Wave execution phase** (task.status = "in_progress"):
- `executeSubtask()` uses `runSubprocess` (OMP local agent) -- not gRPC dependent
- The abort controller is local -- gRPC going down does NOT abort subtasks
- Worker availability check (`checkWorkerAvailability()`, line 352-364) will fail, causing the task to fail with "No workers available"
- **This is the critical failure path**: If gRPC goes down mid-execution, the next wave's worker check will fail and the entire task fails

**Checkpoint/persist phase**:
- Local JSON persistence (`this.store.save()`) works offline
- `store.saveCheckpoint()` works offline
- gRPC sync (`syncTaskToGrpc()`) silently fails -- no data loss locally, but server is out of sync

**Resume after gRPC recovery**:
- If a task failed due to "No workers available", the user can `/uc resume <task-id>`
- `resumeTask()` (line 736-787) resets failed subtasks to pending and re-executes
- But there is no automatic recovery -- the user must manually resume

#### 4. Connection state event propagation

The `connection_state` event is emitted on disconnect and reconnect:
- `grpc-bridge.ts:221` -- `this.config.onConnectionChange?.(false)` on any error
- `grpc-bridge.ts:188` -- `this.config.onConnectionChange?.(true)` on successful reconnect
- `orchestrator.ts:138-139` -- wires to `this.events.emit("connection_state", ...)`
- `extension.ts:158-162` -- renders in footer

But there is no logic that triggers any recovery action on reconnect. The event is purely informational.

### Recovery Gap Summary

| Scenario | Current Behavior | Expected Behavior |
|---|---|---|
| Server restarts, orchestrator still running | Tasks invisible to server until next state change | Bulk sync of all local tasks to server on reconnect |
| Task fails due to worker check during gRPC outage | Task marked failed, user must manually resume | Skip worker check for local execution, or auto-retry |
| Completed tasks before restart | Never re-synced to server | Re-sync completed tasks (at least latest N) |
| In-progress task during gRPC outage | May fail on worker check | Continue execution (OMP subprocess is local) |
| gRPC reconnects | No recovery action triggered | Trigger bulk sync + optionally resume failed tasks |

### Related Specs

- `.trellis/spec/backend/error-handling.md` -- Documents Python-side `fallback_mode="auto"` with state machine: `grpc_ok -> grpc_failed -> local_active -> grpc_recovered -> grpc_ok`. The OMP TypeScript orchestrator has no equivalent state machine.
- `.trellis/spec/backend/taskservice-grpc-spec.md` -- Documents TaskStore as in-memory `HashMap<String, Task>` (no persistence).

## Caveats / Not Found

- The `upsertTask()` method in grpc-bridge.ts does provide a partial recovery path: if a task doesn't exist on the server, it will be re-created on the next sync. But this is reactive (only on state change), not proactive (on reconnect).
- There is no mechanism to detect that the server's TaskStore has been reset (empty). The orchestrator cannot distinguish "server restarted" from "server has no tasks because none were submitted".
- The restart marker file (`/tmp/uc-grpc-restart-marker`) is the closest thing to a server-restart detection mechanism, but it only triggers transport recreation, not data sync.
