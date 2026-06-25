# OMP RPC Server Spec (uc-rpc-server.ts)

> Executable contracts for the JSONL stdio bridge between Python OmpBridge and TypeScript UCOrchestrator.

---

## 1. Scope / Trigger

- Trigger: Python OmpBridge spawns `bun run uc-rpc-server.ts` as subprocess
- Cross-layer: Python OmpBridge ↔ stdin/stdout JSONL ↔ TypeScript UCOrchestrator ↔ Rust gRPC
- Requires code-spec depth: defines cross-language message contracts, method dispatch, async task lifecycle, event forwarding

---

## 2. Signatures

### TypeScript RPC Server (`packages/uc-orchestrator/src/uc-rpc-server.ts`)

```typescript
class RpcServer {
  constructor()                           // creates UCOrchestrator with stub ExtensionAPI
  async init(): Promise<void>             // restore persisted tasks
  async dispatch(req: JsonRpcRequest): Promise<JsonRpcResponse>
  private async handleMethod(method: string, params: Record<string, unknown>): Promise<unknown>
  private emitEvent(type: string, data: unknown): void
}
```

### UCOrchestrator additions (`packages/uc-orchestrator/src/orchestrator/orchestrator.ts`)

```typescript
// New public methods
createTask(description: string): string                // sync — returns task_id immediately
async runTask(taskId: string, ctx?: ExtensionCommandContext): Promise<void>  // async decomposition + execution
getTaskState(id: string): TaskState | undefined        // read-only accessor
getAllTaskStates(): TaskState[]                         // read-only accessor

// Changed signatures
async submitTask(description: string, ctx?: ExtensionCommandContext): Promise<string>  // was void, ctx now optional
async resumeTask(taskId: string, ctx?: ExtensionCommandContext): Promise<boolean>      // ctx now optional

// Exported types (were private)
export interface TaskState { ... }
export interface SubtaskResult { ... }
```

### Stub context (for RPC server without omp runtime)

```typescript
const stubPi: ExtensionAPI = {
  pi: { settings: { workspaceRoot: process.cwd() } },
  logger: { info/warn/error → stderr },
  sendMessage: () => {},
}

function stubCtx(): ExtensionCommandContext {
  return { cwd: process.cwd(), ui: { notify: () => {}, setWidget: () => {} } }
}
```

---

## 3. Contracts

### JSONL Protocol (newline-delimited over stdin/stdout)

**Every message is one JSON line terminated by `\n`.**

#### Startup: ready event

```json
{"event": "ready"}
```

#### Request (Python → Server)

```json
{"method": "submit_task", "params": {"description": "Fix the bug"}, "id": 1}
```

#### Response (Server → Python)

```json
{"id": 1, "result": {"task_id": "uc-3-mqtes80t"}}
```

#### Error Response

```json
{"id": 1, "error": {"code": -32000, "message": "description is required"}}
```

#### Async Event (Server → Python)

```json
{"event": "task_error", "data": {"task_id": "uc-3-mqtes80t", "error": "Decomposition failed: ..."}}
```

### Method Dispatch Table

| RPC method | Orchestrator call | Return | Blocking? |
|---|---|---|---|
| `submit_task` | `createTask()` + `runTask()` (fire-and-forget) | `{task_id}` | No — returns immediately, runs in background |
| `cancel_task` | `cancelTask(taskId, subtaskId?)` | `{ok: bool}` | Yes |
| `pause_task` | `pauseTask(taskId)` | `{ok: bool}` | Yes |
| `resume_task` | `resumeTask(taskId)` | `{ok: bool}` | Yes |
| `show_status` | `getTaskState()` / `getAllTaskStates()` | `{status, task?}` or `{status, tasks}` | Yes |
| `get_task` | `getTaskState(taskId)` | `{task: {...} \| null}` | Yes |
| `list_tasks` | `getAllTaskStates()` | `{tasks: [...]}` | Yes |
| `shutdown` | `process.exit(0)` via `setImmediate` | `{ok: true}` | No — exits after response |

### Task Serialization Format

```json
{
  "id": "uc-3-mqtes80t",
  "description": "Fix the bug",
  "status": "planning",
  "controlState": "running",
  "createdAt": 1782386301341,
  "completedAt": null,
  "error": null,
  "subtasks": [
    {
      "id": "st-1",
      "description": "Write test",
      "status": "pending",
      "dependsOn": [],
      "result": null,
      "error": null
    }
  ]
}
```

### Error Codes

| Code | Meaning |
|---|---|
| -32700 | Parse error (invalid JSON) |
| -32000 | Server error (method failed, missing params, unknown method) |

### Environment

| Key | Required | Default | Purpose |
|---|---|---|---|
| `GRPC_SERVER_ADDR` | No | `localhost:50051` | gRPC server for GrpcBridge |

---

## 4. Validation & Error Matrix

| Condition | Error / Behavior |
|---|---|
| Missing `description` on `submit_task` | `{error: {code: -32000, message: "description is required"}}` |
| Missing `task_id` on `cancel_task` / `pause_task` / `resume_task` / `get_task` | `{error: {code: -32000, message: "task_id is required"}}` |
| Unknown `task_id` on `cancel_task` / `pause_task` | `{result: {ok: false}}` |
| Unknown `task_id` on `get_task` | `{result: {task: null}}` |
| Unknown `task_id` on `show_status` | `{result: {status: "not_found"}}` |
| Unknown method | `{error: {code: -32000, message: "Unknown method: ..."}}` |
| Invalid JSON input | `{id: 0, error: {code: -32700, message: "Parse error"}}` |
| `runTask` fails (decomposition error) | Event: `{event: "task_error", data: {task_id, error}}` |
| stdin closes | `process.exit(0)` |

---

## 5. Good/Base/Bad Cases

**Good**: Python spawns `bun run uc-rpc-server.ts` → receives `{"event":"ready"}` → `submit_task` → immediate `task_id` → task runs in background → `list_tasks` shows progress → `shutdown`

**Base**: Python spawns → `ready` → `list_tasks` returns empty → `shutdown`. No tasks submitted.

**Bad**: Python spawns → `submit_task` → `runTask` throws → `task_error` event emitted → Python can still call other methods

---

## 6. Tests Required

| Test | Assertion Points |
|---|---|
| `list_tasks` empty | Returns `{tasks: []}` |
| `get_task` missing | Returns `{task: null}` |
| `cancel_task` missing | Returns `{ok: false}` |
| `pause_task` missing | Returns `{ok: false}` |
| `resume_task` missing | Returns `{ok: false}` |
| Unknown method | Returns error with code -32000 |
| `show_status` without task_id | Returns `{status: "ok", tasks: [...]}`
| `show_status` missing task | Returns `{status: "not_found"}` |
| `shutdown` | Returns `{ok: true}` |
| Missing params | Returns error mentioning required field |
| Smoke: server emits ready | `{"event":"ready"}` on stdout first |
| Smoke: `list_tasks` round-trip | Command in → response out |
| Smoke: `submit_task` returns immediately | task_id in response, task runs in background |

---

## 7. Wrong vs Correct

### Wrong: Block RPC response until task completes

```typescript
// BAD — Python waits for entire decomposition + execution (minutes)
async handleMethod(method, params) {
  case "submit_task": {
    const taskId = await this.orchestrator.submitTask(description, ctx); // blocks!
    return { task_id: taskId };
  }
}
```

### Correct: Return task_id immediately, run in background

```typescript
// GOOD — Python gets task_id instantly, task lifecycle runs async
case "submit_task": {
  const taskId = this.orchestrator.createTask(description); // sync, instant
  this.orchestrator.runTask(taskId, stubCtx()).catch((err) => {
    this.emitEvent("task_error", { task_id: taskId, error: err.message });
  });
  return { task_id: taskId };
}
```

---

## Design Decision: createTask / runTask split

**Context**: RPC protocol requires immediate response, but `submitTask` blocks for the entire task lifecycle.

**Options**:
1. Make `submitTask` async and let Python wait — blocks RPC channel, Python can't send other commands
2. Split into `createTask` (sync) + `runTask` (async) — chosen
3. Thread-per-request — overengineered for single-client JSONL

**Decision**: Split. `createTask` creates the TaskState and returns the ID synchronously. `runTask` handles decomposition + execution asynchronously. Errors from `runTask` are forwarded as JSONL events.

---

## Architecture: Bridge Chain

```
Python OmpBridge ──JSONL stdio──▸ uc-rpc-server.ts (Bun)
                                     │
                                     ├─ UCOrchestrator (TS)
                                     │    ├─ TaskStore (local JSON)
                                     │    └─ GrpcBridge (HTTP JSON)
                                     │         │
                                     │         ▼
                                     └──▸ Rust gRPC Server (:50051)
                                              │
                                              ├─ TaskStore (in-memory)
                                              ├─ LocalWorkerBridge (Rust↔Python JSON-RPC)
                                              └─ EventStore (NATS/fallback)
```

Two JSONL bridge protocols exist:
1. **uc-rpc-server.ts** (this spec): Python ↔ TypeScript, for OMP orchestrator control
2. **Local Worker Bridge** (local-worker-bridge-spec.md): Rust ↔ Python, for task execution

Both use newline-delimited JSON but different protocols (JSON-RPC 2.0 vs simple JSONL).

---

## Control Signal Flow (TUI/Dashboard → Orchestrator)

When TUI or Dashboard calls PauseTask/ResumeTask/CancelTask via gRPC:

```
TUI pause ─▸ gRPC PauseTask ─▸ NATS uc.task.event(task_paused) ─▸ ControlSignalSubscriber ─▸ orchestrator.pauseTask()
TUI resume ─▸ gRPC ResumeTask ─▸ NATS uc.task.event(task_resumed) ─▸ ControlSignalSubscriber ─▸ orchestrator.resumeTask()
TUI cancel ─▸ gRPC CancelTask ─▸ NATS uc.task.event(task_cancelled) ─▸ ControlSignalSubscriber ─▸ orchestrator.cancelTask()
```

**Fallback (NATS unavailable):**
```
TUI pause ─▸ gRPC PauseTask ─▸ TaskStore (paused) ─▸ polling detect ─▸ orchestrator.pauseTask()
```

### ControlSignalSubscriber (`control-signal-subscriber.ts`)

- Subscribes to NATS `uc.task.event` subject
- Filters for `task_paused`, `task_resumed`, `task_cancelled` events
- Dedup: message_id based, 5-minute window, 10K entry cap
- Polling fallback: 2s interval, GrpcBridge.getTask() per active task
- Start is non-blocking (2s NATS connect timeout)
- Guard against cancel false-positives in polling (only cancel if task still active in Orchestrator)
