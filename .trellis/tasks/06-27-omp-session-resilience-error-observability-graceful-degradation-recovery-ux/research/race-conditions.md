# Research: Race Conditions

- **Query**: Check for concurrent access to tasks Map, AbortController lifecycle issues, and multiple reconnect attempts
- **Scope**: internal
- **Date**: 2026-06-27

## Findings

### Files Found

| File Path | Description |
|---|---|
| `packages/uc-orchestrator/src/orchestrator/orchestrator.ts` | Core orchestrator with concurrent wave execution |
| `packages/uc-orchestrator/src/orchestrator/grpc-bridge.ts` | Reconnect guard |
| `packages/uc-orchestrator/src/orchestrator/control-signal-subscriber.ts` | NATS/polling with concurrent handlers |

### 1. Concurrent Access to `tasks` Map

The `tasks` Map (`orchestrator.ts:112`) is accessed from multiple async contexts:

**Writers**:
- `submitTask()` (line 188) -- sets task
- `createTask()` (line 252) -- sets task
- `executeWaves()` (lines 334-481) -- modifies task.status, task.subtasks, task.controlState
- `executeWave()` (lines 483-601) -- modifies subtask status/result/error via `task.subtasks.find()`
- `cancelTask()` (lines 682-721) -- modifies task.controlState, task.status, subtask.status
- `pauseTask()` (lines 723-734) -- modifies task.controlState
- `resumeTask()` (lines 736-787) -- modifies task.status, task.controlState, subtask.status
- `evictCompletedTasks()` (lines 1329-1349) -- deletes from tasks Map

**Readers**:
- `showStatus()` (line 809) -- reads tasks
- `getTaskState()` (line 1286) -- reads tasks
- `getAllTaskStates()` (line 1290) -- reads tasks
- `getActiveTaskIds()` (line 1295) -- reads tasks

**Race scenario 1: cancelTask while executeWaves is running**

`cancelTask()` (line 682) sets `task.controlState = "cancelled"` and calls `abortCtrl?.abort()`. Meanwhile, `executeWaves()` checks `task.controlState` at the start of each wave (line 336). But within `executeWave()`, the `runNext()` loop checks `task.controlState === "cancelled"` (line 504) before picking the next subtask.

Since JavaScript is single-threaded (event loop), there is no true data race -- the Map mutations are atomic at the JS level. However, there are **logical races**:

- `cancelTask()` sets `task.controlState = "cancelled"` and `task.status = "cancelled"` (lines 701-702). Then it marks running subtasks as cancelled (lines 709-714). But `executeWave()` may have already read `task.controlState` as "running" and started a new subtask. The subtask will only check cancel at the next iteration of `runNext()` (line 504).
- Between `cancelTask()` setting `controlState = "cancelled"` and the next `runNext()` iteration, a subtask may start executing via `runSubprocess()`. The abort signal will eventually abort it, but there is a window where the subtask is running after cancel was requested.

**Race scenario 2: evictCompletedTasks while reading**

`evictCompletedTasks()` (line 1329) deletes entries from `this.tasks`. This is called at the end of `executeWaves()` (line 480). Meanwhile, `getAllTaskStates()` (line 1290) iterates `this.tasks.values()`. Since JS is single-threaded, the iteration won't be interrupted mid-way. But if `evictCompletedTasks` runs between two reads by the UI, the UI may see inconsistent state (task exists, then disappears).

**Race scenario 3: resumeTask while executeWaves is still running**

`resumeTask()` (line 736) checks `task.controlState !== "paused"` and then sets it to "running". It then calls `executeWaves()` (line 785). But if the previous `executeWaves()` call is still in the process of returning (e.g., after pause, the function returns at line 347), there could be two concurrent `executeWaves()` calls for the same task. The pause path returns early (line 347: `return`), so this should be safe. But if the timing is such that resume is called before the pause return completes, two execution chains could be running.

### 2. AbortController Lifecycle

**Creation**: `AbortController` is created in `submitTask()` (line 189) and `createTask()` (line 253) and `resumeTask()` (line 745).

**Usage**: The signal is passed to `runSubprocess()` in `executeSubtask()` (line 1057):
```typescript
signal: abortCtrl?.signal ?? AbortSignal.timeout(this.config.subtaskTimeoutMs),
```

**Abort**: Called in `cancelTask()` (line 706) and `destroy()` (line 1311).

**Removal**: `AbortController` is removed in `evictCompletedTasks()` (line 1344) and `destroy()` (line 1314).

**Race scenario 4: abort after controller was removed**

If `evictCompletedTasks()` deletes the `AbortController` from the Map (line 1344), and then a delayed cancel request comes in (e.g., from NATS control signal), `cancelTask()` will find no `AbortController`:
```typescript
const abortCtrl = this.abortControllers.get(taskId);
abortCtrl?.abort(); // undefined -- no-op
```
This is safe (optional chaining), but the task's running subprocess won't be aborted. The task was already evicted from the Map, so `this.tasks.get(taskId)` would also return undefined, and `cancelTask()` would return false (line 684).

**Race scenario 5: resumeTask replaces AbortController**

`resumeTask()` (line 745) creates a new `AbortController`:
```typescript
this.abortControllers.set(taskId, new AbortController());
```
If a subtask from the previous execution is still running (e.g., a long-running subprocess that hasn't responded to the old abort signal), the new controller won't abort it. The old subprocess becomes orphaned.

### 3. Multiple Reconnect Attempts

**Guard in grpc-bridge.ts**: The `reconnecting` flag (line 112) prevents concurrent reconnect attempts:
```typescript
private reconnecting = false;

private async tryReconnect(err: unknown): Promise<boolean> {
  if (!this.isConnectionError(err)) return false;
  if (this.reconnecting) return false;  // Guard
  this.reconnecting = true;
  // ...
  finally { this.reconnecting = false; }
}
```

This guard works for sequential calls to `tryReconnect()`. However:

**Race scenario 6: Multiple withReconnect calls triggering reconnect**

If multiple RPC calls fail simultaneously (e.g., `listWorkers()` and `readMemory()` both fail), each will call `withReconnect()`, which calls `tryReconnect()`. The `reconnecting` flag prevents concurrent reconnects, but:
- The first call triggers reconnect and waits for health check
- The second call sees `reconnecting = true` and returns `false` immediately
- The second call falls through to the fallback value
- This is correct behavior -- only one reconnect is attempted at a time

**Race scenario 7: checkRestartMarker during reconnect**

`checkRestartMarker()` (line 198-209) uses `readFileSync` (synchronous) and calls `this.reconnect()` directly (not `tryReconnect()`). If a reconnect is in progress (via `tryReconnect()`), `checkRestartMarker()` could call `this.reconnect()` concurrently, recreating the transport while `tryReconnect()` is verifying the health check on the old transport. However, since `checkRestartMarker()` is called at the start of `health()` (line 236), and `health()` is typically called from a single health check loop, this is unlikely in practice.

### 4. ControlSignalSubscriber Concurrent Handlers

**Race scenario 8: Multiple NATS events for the same task**

`handleNatsEvent()` (line 155) processes events sequentially within the NATS subscription loop. But the handler calls (`this.handler.pauseTask()`, etc.) are async and not awaited -- they use `.catch()` (lines 183-197). If two events arrive quickly (e.g., pause then resume), both handlers will be invoked concurrently:
```typescript
case "task_paused":
  this.handler.pauseTask(task_id).catch(...);
  break;
case "task_resumed":
  this.handler.resumeTask(task_id).catch(...);
  break;
```
Both `pauseTask()` and `resumeTask()` modify `task.controlState`. Since they are async and not awaited, they could execute in any order depending on the event loop scheduling. The dedup mechanism (line 164-177) prevents duplicate events, but not rapid sequential different events.

**Race scenario 9: Polling fallback concurrent with NATS**

If NATS reconnects after being in polling mode, both the polling timer and the NATS subscription could detect state changes simultaneously. The `startNatsSubscription()` method does not stop the polling timer. Looking at the code:
- `start()` (line 74-87): starts NATS OR polling, not both
- `tryNatsReconnect()` (line 137-153): on successful reconnect, calls `startNatsSubscription()` but does NOT stop polling
- **Bug**: After NATS reconnect, both polling AND NATS subscription are active, leading to duplicate control signal processing.

### Summary of Race Conditions

| ID | Severity | Description |
|---|---|---|
| 1 | Low | cancelTask during executeWaves -- subtask may start before cancel propagates (safe due to JS single-thread, but has logical latency) |
| 2 | Low | evictCompletedTasks during UI reads -- UI may see inconsistent state between reads |
| 3 | Medium | resumeTask could overlap with paused executeWaves return -- potential double execution |
| 4 | Low | abort after controller evicted -- safe (optional chaining), but subprocess not killed |
| 5 | Medium | resumeTask replaces AbortController -- old subprocess may be orphaned |
| 6 | None | Multiple withReconnect -- guard works correctly |
| 7 | Low | checkRestartMarker during reconnect -- unlikely in practice |
| 8 | Medium | Concurrent NATS control events for same task -- handlers not awaited, order not guaranteed |
| 9 | **HIGH** | NATS reconnect does not stop polling timer -- both active simultaneously, duplicate processing |

### Related Specs

- `.trellis/spec/backend/nats-bridge-spec.md` -- Documents the NATS architecture but does not address the polling/NATS overlap issue.

## Caveats / Not Found

- JavaScript's single-threaded event loop prevents true data races (no concurrent memory access). The race conditions identified are logical races (ordering of async operations) rather than data races.
- The severity assessments assume production usage patterns. Some scenarios may be extremely unlikely in practice.
