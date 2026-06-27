# Research: gRPC Sync Reliability

- **Query**: Is syncTaskToGrpc fire-and-forget appropriate? Should upsertTask use withReconnect? Are there cases where sync should be awaited?
- **Scope**: internal
- **Date**: 2026-06-27

## Findings

### Files Found

| File Path | Description |
|---|---|
| `packages/uc-orchestrator/src/orchestrator/grpc-bridge.ts` | gRPC client with reconnect logic |
| `packages/uc-orchestrator/src/orchestrator/orchestrator.ts` | syncTaskToGrpc + upsertTask usage |

### syncTaskToGrpc Implementation

`orchestrator.ts:1250-1255`:
```typescript
private syncTaskToGrpc(task: TaskState): void {
  // ponytail: fire-and-forget -- don't await, don't block orchestrator
  this.bridge.upsertTask(this.toPersisted(task)).catch(() => {
    // gRPC sync is best-effort; failure is non-fatal
  });
}
```

This is called at the following points:
- `submitTask()` line 207 -- after decomposition failure
- `submitTask()` line 223 -- after task decomposed (status = in_progress)
- `executeWaves()` line 386 -- after each wave completes
- `executeWaves()` line 453 -- at task completion/failure
- `executeWave()` line 568 -- after each subtask completes (within the wave loop)
- `cancelTask()` line 695 -- after subtask-level cancel
- `cancelTask()` line 717 -- after task-level cancel
- `pauseTask()` line 730 -- after pause
- `resumeTask()` line 746 -- after resume state change
- `tryRedecompose()` line 667 -- after re-decomposition
- `executeSubtaskWithRetry()` line 1009 -- after permanent failure

### upsertTask Implementation

`grpc-bridge.ts:313-348`:
```typescript
async upsertTask(task: PersistedTask): Promise<boolean> {
  return this.withReconnect(async () => {
    const existing = await this.getTask(task.id);
    if (existing) {
      const resp = await this.taskClient.updateTask(
        create(UpdateTaskRequestSchema, { taskId: task.id, status: task.status, subtasks: ... }),
      );
      return resp.success;
    }
    // Task doesn't exist on server -- re-create via submitTask
    const resp = await this.taskClient.submitTask(
      create(SubmitTaskRequestSchema, { description: task.description, projectId: "" }),
    );
    return resp.success;
  }, false);
}
```

**Critical observation**: `upsertTask` uses `withReconnect()` which:
1. Attempts the RPC call
2. On connection error, tries one reconnect then retries
3. If both fail (or non-connection error), returns the fallback value (`false`)
4. The `false` return is then caught by `syncTaskToGrpc()`'s `.catch(() => {})` and silently discarded

### Analysis: When Should sync Be Awaited?

**Case 1: Task status transitions (in_progress -> completed, in_progress -> failed)**

Currently fire-and-forget. If the sync fails:
- Local state: task is completed/failed
- Server state: task is still in_progress (or doesn't exist)
- Dashboard/TUI: shows stale state
- **Should this be awaited?**: No -- the orchestrator should not block on gRPC sync. The local state is authoritative. However, the failure should be logged (currently silently swallowed).

**Case 2: Task creation (planning -> in_progress)**

Currently fire-and-forget. If the sync fails:
- Local state: task exists with subtasks
- Server state: no record of the task
- Subsequent `upsertTask` calls will create it (getTask returns null, submitTask is called)
- **Issue**: The `submitTask` call on the server creates a task with the description but no subtasks (the SubmitTaskRequest only has description + projectId). The server's decomposition is newline-based, which will produce different subtask IDs and structure.
- **Should this be awaited?**: For the initial sync, yes -- or at minimum, the failure should trigger a retry. If the initial creation fails, subsequent upsert attempts will create a mismatched task on the server.

**Case 3: Cancel/pause/resume state changes**

Currently fire-and-forget. If the sync fails:
- Local state: task is cancelled/paused
- Server state: task is still in its previous state
- The orchestrator also calls `bridge.pauseTask()` and `bridge.resumeTask()` as fire-and-forget (lines 731, 746)
- These bridge calls go directly to the server's PauseTask/ResumeTask RPCs
- If the server RPC succeeds but the `syncTaskToGrpc` fails, the server's TaskStore has the correct control state but stale subtask data
- **Should this be awaited?**: The bridge.pauseTask/resumeTask calls should be awaited (they set the server state). The syncTaskToGrpc can remain fire-and-forget but should log failures.

**Case 4: Subtask-level state changes (within executeWave)**

Currently fire-and-forget at line 568. Called for every subtask completion.
- If the sync fails for one subtask, the server is out of sync for that subtask
- But the next subtask's sync will include the full task state (upsert sends the whole task)
- **Issue**: `upsertTask` calls `getTask` first, then `updateTask` with the current subtask list. If the server has stale data, the update will overwrite it with the current (correct) data.
- **Should this be awaited?**: No -- the next sync will correct the state. But frequency matters -- if gRPC is flaky, many syncs will fail and the server may be significantly behind.

### Analysis: Should upsertTask Use withReconnect?

**Current behavior**: `upsertTask` DOES use `withReconnect()` (line 314). It attempts the call, reconnects on connection error, retries once, and falls back to `false`.

**Issue with withReconnect in upsertTask**: The `withReconnect` fallback returns `false`, and this `false` is caught by `syncTaskToGrpc`'s `.catch(() => {})`. So:
1. If the first attempt fails due to a non-connection error (e.g., server rejected the update), `withReconnect` does NOT try to reconnect (only connection errors trigger reconnect). It returns `false`.
2. If the first attempt fails due to a connection error, `withReconnect` tries reconnect and retry. If the retry also fails, it returns `false`.
3. In both cases, the orchestrator has no way to distinguish "sync failed temporarily" from "sync failed permanently" from "sync succeeded".

**Should upsertTask be awaited for critical operations?**: For the initial task creation (status transition from planning to in_progress), awaiting upsertTask would ensure the server has the correct task record before execution proceeds. This is a design trade-off:
- Awaiting: blocks orchestrator on gRPC, adds latency, but ensures server is in sync
- Fire-and-forget: no latency, but server may be out of sync

### The SubmitTask Description-Only Problem

The most significant issue with `upsertTask` is the re-creation path (lines 340-347):
```typescript
const resp = await this.taskClient.submitTask(
  create(SubmitTaskRequestSchema, {
    description: task.description,
    projectId: "",
  }),
);
return resp.success;
```

When the task doesn't exist on the server (e.g., after a server restart), `upsertTask` re-creates it via `submitTask`. But `SubmitTaskRequest` only has `description` and `projectId` -- it does NOT carry:
- The original task ID (the server generates a new UUID)
- The subtask definitions
- The task status
- The controlState

This means:
1. The server creates a new task with a different ID
2. The server decomposes the description using its own newline-based heuristic
3. The resulting task on the server has different subtask IDs and structure
4. Future `updateTask` calls using the ORIGINAL task ID will fail (server has a different ID)
5. The orchestrator's `upsertTask` calls `getTask(task.id)` which won't find the server-created task (different ID)

**This is a fundamental mismatch**: The orchestrator and the server have different task IDs for the same task after a server restart.

### Related Specs

- `.trellis/spec/backend/taskservice-grpc-spec.md` -- Documents SubmitTaskRequest fields (description + projectId only) and the in-memory TaskStore.
- `.trellis/spec/backend/nats-bridge-spec.md` -- Known limitation #1: "Orchestrator creates its own task_id, ignoring the one from NATS submit. The gRPC TaskStore's placeholder task stays in Planning forever."

## Caveats / Not Found

- The `withReconnect` pattern in `upsertTask` is correct for retry-on-connection-error. The issue is not the reconnect logic itself, but the silent swallowing of failures and the description-only re-creation path.
- The task ID mismatch after server restart is a known architectural limitation documented in the NATS bridge spec.
