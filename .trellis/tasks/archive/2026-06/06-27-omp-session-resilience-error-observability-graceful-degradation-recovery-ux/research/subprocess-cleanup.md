# Research: Subprocess Cleanup

- **Query**: When a subtask is cancelled, is the subprocess actually killed? Does the abort signal properly propagate? Are there orphaned subprocesses?
- **Scope**: internal
- **Date**: 2026-06-27

## Findings

### Files Found

| File Path | Description |
|---|---|
| `packages/uc-orchestrator/src/orchestrator/orchestrator.ts` | Task orchestration with subprocess execution |
| `packages/uc-orchestrator/src/orchestrator/grpc-bridge.ts` | Not directly involved in subprocess management |

### Subprocess Execution Model

The orchestrator uses OMP's `runSubprocess()` API (imported from `@oh-my-pi/pi-coding-agent` at line 17) to execute subtasks. This is NOT a raw Node.js `child_process.spawn()` -- it is an OMP framework function that manages the subprocess lifecycle.

### Subprocess Creation

**Decomposition** (`orchestrator.ts:861-913`):
```typescript
const result = await runSubprocess({
  cwd: ctx.cwd,
  agent: { name: "decomposer", ... },
  task: description,
  id: `decompose-${Date.now().toString(36)}`,
  index: 0,
  signal: AbortSignal.timeout(120_000),  // 2-minute timeout
  // ...
});
```

**Subtask execution** (`orchestrator.ts:1046-1061`):
```typescript
const subResult = await runSubprocess({
  cwd: ctx.cwd,
  agent: { name: "worker", ... },
  task: taskPrompt,
  id: def.id,
  index: 0,
  signal: abortCtrl?.signal ?? AbortSignal.timeout(this.config.subtaskTimeoutMs),
  // ...
});
```

**Review** (`orchestrator.ts:1122-1150`):
```typescript
const result = await runSubprocess({
  cwd: ctx.cwd,
  agent: { name: "supervisor", ... },
  task: [...],
  id: `review-${def.id}`,
  index: 0,
  signal: AbortSignal.timeout(this.config.reviewTimeoutMs),
  // ...
});
```

### Abort Signal Propagation

**Task-level abort**: When `cancelTask()` is called (line 682-721):
1. `task.controlState = "cancelled"` (line 701)
2. `task.status = "cancelled"` (line 702)
3. `abortCtrl?.abort()` (line 706) -- aborts the `AbortController` for this task
4. Running subtasks are marked as cancelled (lines 709-714)

The abort signal flows:
- `cancelTask()` -> `abortCtrl.abort()` -> `AbortController.signal` aborted -> `runSubprocess({ signal: abortCtrl?.signal })` receives the abort

**Key question**: Does `runSubprocess()` actually kill the subprocess when the signal is aborted?

The `runSubprocess` function is from the OMP framework (`@oh-my-pi/pi-coding-agent`). The orchestrator passes the signal but does NOT directly manage the subprocess. The OMP framework is responsible for:
1. Starting the subprocess (likely a Claude Code or similar coding agent)
2. Passing the abort signal to the subprocess
3. Killing the subprocess when the signal fires

**What we can verify from the orchestrator code**:
- The signal IS passed to `runSubprocess()` (line 1057)
- The orchestrator DOES call `abortCtrl?.abort()` on cancel (line 706)
- The orchestrator DOES handle `AbortError` in the catch block (line 1095-1096):
  ```typescript
  if ((err as Error).name === "AbortError") {
    result.status = "cancelled";
  }
  ```

**What we cannot verify**:
- Whether `runSubprocess()` actually terminates the subprocess process on signal abort
- Whether the subprocess (Claude Code agent) respects the abort signal
- Whether there is a grace period before force-killing

### Orphaned Subprocess Scenarios

**Scenario 1: Eviction removes AbortController before subprocess completes**

`evictCompletedTasks()` (line 1329-1349) deletes `AbortController` from the Map (line 1344):
```typescript
this.abortControllers.delete(terminalIds[i].id);
```
If a subprocess is still running (unlikely for terminal tasks, but possible if the task was marked completed while a subprocess hasn't fully exited), the AbortController is removed and can no longer be used to abort the subprocess.

**Scenario 2: resumeTask replaces AbortController**

`resumeTask()` (line 745) creates a new `AbortController`:
```typescript
this.abortControllers.set(taskId, new AbortController());
```
If a subprocess from the previous execution is still running (e.g., a subprocess that was started before pause and hasn't exited yet), the new AbortController cannot abort it. The old subprocess is orphaned.

**Scenario 3: destroy() aborts all but doesn't wait**

`destroy()` (line 1307-1323) aborts all controllers:
```typescript
for (const ctrl of this.abortControllers.values()) {
  ctrl.abort();
}
this.abortControllers.clear();
```
But it does NOT wait for subprocesses to actually exit. If `runSubprocess()` is async and needs time to clean up, the abort signal may not be processed before the orchestrator is destroyed.

**Scenario 4: Decomposition subprocess uses AbortSignal.timeout(), not task AbortController**

The decomposition subprocess (line 895) uses `AbortSignal.timeout(120_000)`, not the task's `AbortController`:
```typescript
signal: AbortSignal.timeout(120_000),
```
This means:
- If the task is cancelled during decomposition, the decomposition subprocess is NOT aborted
- The decomposition will continue for up to 2 minutes even after cancel
- The decomposition subprocess is orphaned on cancel

**Scenario 5: Review subprocess uses AbortSignal.timeout(), not task AbortController**

The review subprocess (line 1146) also uses `AbortSignal.timeout(this.config.reviewTimeoutMs)`:
```typescript
signal: AbortSignal.timeout(this.config.reviewTimeoutMs),
```
Same issue as decomposition -- review subprocess is not aborted on task cancel.

**Scenario 6: executeWave runningCount tracking**

`executeWave()` uses `this.runningCount` (line 592, 558) to track concurrency:
```typescript
this.runningCount++;  // line 592
// ...
this.runningCount--;  // line 558
```
If a subprocess is orphaned (still running but not tracked), `runningCount` will be incorrect. This could affect future scheduling decisions. However, `runningCount` is only used for tracking, not for gating execution.

### Subprocess Kill Verification

The orchestrator has NO explicit subprocess kill mechanism. It relies entirely on:
1. `AbortController.signal` passed to `runSubprocess()`
2. OMP framework's handling of the abort signal
3. Timeout signals (`AbortSignal.timeout()`)

There is no:
- Process ID tracking
- `child_process.kill()` calls
- Force-kill timeout after graceful abort
- Subprocess health monitoring

### Summary

| Scenario | Subprocess Killed? | Orphaned? | Severity |
|---|---|---|---|
| Task cancel during subtask execution | Yes (via AbortController) | No | None |
| Task cancel during decomposition | No (uses timeout, not AbortController) | **Yes** -- up to 2min | Medium |
| Task cancel during review | No (uses timeout, not AbortController) | **Yes** -- up to 60s | Medium |
| Eviction removes AbortController | N/A (task is terminal) | Unlikely | Low |
| resumeTask replaces AbortController | Old subprocess orphaned | **Yes** | Medium |
| destroy() during execution | Abort sent but not awaited | **Possible** | Low |

### Related Specs

- `.trellis/spec/backend/scheduler-spec.md` -- May contain subprocess management contracts.

## Caveats / Not Found

- The actual subprocess kill behavior depends on `runSubprocess()` from `@oh-my-pi/pi-coding-agent`, which is not in this repository. The analysis is based on the signal passing pattern, not the actual implementation.
- It is possible that `runSubprocess()` handles abort signals correctly (kills the subprocess, waits for exit, etc.). The orphan scenarios are based on the assumption that it does NOT.
- The decomposition and review subprocesses using `AbortSignal.timeout()` instead of the task's `AbortController` is a clear design issue regardless of `runSubprocess()` behavior.
