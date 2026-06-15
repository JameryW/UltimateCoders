# TaskService gRPC Spec

> Executable contracts for the TaskService gRPC layer — proto definitions, Rust server implementation, and Node.js client.

---

## 1. Scope / Trigger

- Trigger: TUI PR2 — gRPC integration for task/subtask/event operations
- Cross-layer: proto → Rust server → Node.js client → React hooks → Ink TUI
- Requires code-spec depth because it defines cross-layer request/response contracts and streaming behavior

---

## 2. Signatures

### Proto Service (`crates/uc-grpc/proto/engine.proto`)

```protobuf
service TaskService {
  rpc SubmitTask(SubmitTaskRequest) returns (SubmitTaskResponse);
  rpc GetTask(GetTaskRequest) returns (GetTaskResponse);
  rpc ListTasks(ListTasksRequest) returns (ListTasksResponse);
  rpc WatchTask(WatchTaskRequest) returns (stream TaskEvent);
  rpc PauseTask(PauseTaskRequest) returns (PauseTaskResponse);
  rpc ResumeTask(ResumeTaskRequest) returns (ResumeTaskResponse);
}
```

### Rust Server (`crates/uc-grpc/src/server.rs`)

```rust
impl<E: EngineApi + Send + Sync + 'static> TaskService for GrpcServer<E> {
    async fn submit_task(&self, request: Request<SubmitTaskRequest>) -> Result<Response<SubmitTaskResponse>, Status>;
    async fn get_task(&self, request: Request<GetTaskRequest>) -> Result<Response<GetTaskResponse>, Status>;
    async fn list_tasks(&self, request: Request<ListTasksRequest>) -> Result<Response<ListTasksResponse>, Status>;
    async fn watch_task(&self, request: Request<WatchTaskRequest>) -> Result<Response<Pin<Box<dyn Stream<Item = Result<TaskEvent, Status>> + Send>>>, Status>;
    async fn pause_task(&self, request: Request<PauseTaskRequest>) -> Result<Response<PauseTaskResponse>, Status>;
    async fn resume_task(&self, request: Request<ResumeTaskRequest>) -> Result<Response<ResumeTaskResponse>, Status>;
}
```

### TaskStore (`crates/uc-grpc/src/server.rs`)

```rust
pub struct TaskStore {
    tasks: Mutex<HashMap<String, Task>>,
    events: Mutex<Vec<AgentEventType>>,
}
```

Key methods:
- `submit_task(description, project_id) -> (Task, Vec<Subtask>)` — creates task + decomposes
- `get_task(task_id) -> Option<Task>`
- `list_tasks() -> Vec<Task>`
- `pause_task(task_id) -> Result<(), String>` — validates state (only InProgress/Planning)
- `resume_task(task_id) -> Result<(), String>` — validates state (only Paused)
- `read_events_from(offset) -> Vec<(usize, &AgentEventType)>`

### Node.js Client (`tui/src/grpc/client.ts`)

```typescript
class TaskServiceClient {
    constructor(serverAddress?: string);
    submitTask(request: SubmitTaskRequest): Promise<SubmitTaskResponse>;
    getTask(request: GetTaskRequest): Promise<GetTaskResponse>;
    listTasks(request: ListTasksRequest): Promise<ListTasksResponse>;
    watchTask(request: WatchTaskRequest): ClientReadableStream<TaskEvent>;
    pauseTask(request: PauseTaskRequest): Promise<PauseTaskResponse>;
    resumeTask(request: ResumeTaskRequest): Promise<ResumeTaskResponse>;
    close(): void;
}
```

---

## 3. Contracts

### Request Fields

| RPC | Field | Type | Constraints |
|-----|-------|------|-------------|
| SubmitTask | `description` | string | Required, non-empty |
| SubmitTask | `project_id` | string | Optional, defaults to "" |
| GetTask | `task_id` | string | Required, non-empty |
| ListTasks | `project_id` | string | Optional, "" = all |
| WatchTask | `task_id` | string | Optional, "" = all tasks |
| PauseTask | `task_id` | string | Required, non-empty |
| ResumeTask | `task_id` | string | Required, non-empty |

### Response Fields

| RPC | Field | Type | Notes |
|-----|-------|------|-------|
| SubmitTask | `success` | bool | true if task created + decomposed |
| SubmitTask | `task_id` | string | UUID of created task |
| SubmitTask | `status` | string | TaskStatus name |
| SubmitTask | `subtask_count` | int32 | Number of decomposed subtasks |
| SubmitTask | `subtasks` | repeated SubtaskProto | Subtask list |
| SubmitTask | `error` | string | Error message if success=false |
| GetTask | `task` | TaskProto (optional) | None if not found |
| ListTasks | `tasks` | repeated TaskProto | All matching tasks |
| WatchTask | stream TaskEvent | Server-streaming | Polls every 500ms |
| PauseTask | `success` | bool | false if invalid state |
| PauseTask | `error` | string | Reason if failed |
| ResumeTask | `success` | bool | false if invalid state |
| ResumeTask | `error` | string | Reason if failed |

### Environment Keys

| Key | Required | Default | Purpose |
|-----|----------|---------|---------|
| `UC_GRPC_ADDR` | No | `[::]:50051` | Rust server listen address |
| `GRPC_SERVER_ADDR` | No | `localhost:50051` | Node.js client connect address |
| `GRPC_PROTO_PATH` | No | (relative resolve) | Path to engine.proto for proto-loader |

---

## 4. Validation & Error Matrix

### PauseTask State Validation

| Current State | Result | Error Message |
|---------------|--------|---------------|
| InProgress | ✅ Paused | — |
| Planning | ✅ Paused | — |
| Created | ❌ Rejected | "Cannot pause task in Created state" |
| Completed | ❌ Rejected | "Cannot pause task in Completed state" |
| Failed | ❌ Rejected | "Cannot pause task in Failed state" |
| Paused | ❌ Rejected | "Task is already paused" |

### ResumeTask State Validation

| Current State | Result | Error Message |
|---------------|--------|---------------|
| Paused | ✅ InProgress | — |
| InProgress | ❌ Rejected | "Task is not paused" |
| Created | ❌ Rejected | "Task is not paused" |
| Completed | ❌ Rejected | "Task is not paused" |
| Failed | ❌ Rejected | "Task is not paused" |

### gRPC Status Code Mapping (TaskService-specific)

| Condition | tonic Code |
|-----------|------------|
| Task not found | `NotFound` |
| Invalid state transition (pause/resume) | `FailedPrecondition` |
| Empty description in SubmitTask | `InvalidArgument` |
| Internal task store error | `Internal` |

---

## 5. Good/Base/Bad Cases

### SubmitTask

- **Good**: `"Fix the login bug"` → success=true, task_id=UUID, subtask_count=3 (decomposed by newline)
- **Base**: `"Single task"` → success=true, task_id=UUID, subtask_count=1 (no newlines, single subtask)
- **Bad**: `""` → success=false, error="Task description cannot be empty"

### WatchTask

- **Good**: task_id="abc" → stream of TaskEvents for that task
- **Base**: task_id="" → stream of ALL TaskEvents (global watch)
- **Bad**: server down → client receives `error` event, hook sets `isConnected=false`

### PauseTask

- **Good**: task in InProgress → success=true, status=Paused
- **Base**: task not found → success=false, error="Task not found"
- **Bad**: task in Completed → success=false, error="Cannot pause task in Completed state"

---

## 6. Tests Required

### Rust Unit Tests (in `crates/uc-grpc/src/server.rs`)

| Test | Assertion |
|------|-----------|
| `task_store_submit` | Task created with correct description, status=InProgress, subtasks decomposed |
| `task_store_get` | Retrieved task matches submitted task |
| `task_store_list` | List returns all submitted tasks |
| `task_store_pause_valid` | InProgress task can be paused |
| `task_store_pause_invalid` | Completed task cannot be paused (returns Err) |
| `task_store_resume_valid` | Paused task can be resumed |
| `task_store_resume_invalid` | InProgress task cannot be resumed (returns Err) |
| `task_store_events` | SubmitTask emits TaskCreated event + SubtaskAssigned events |
| `decompose_task_single` | Single-line description → 1 subtask |
| `decompose_task_multi` | Multi-line description → N subtasks (one per line) |

### Rust Integration Tests (in `crates/uc-grpc/tests/grpc_integration.rs`)

| Test | Assertion |
|------|-----------|
| `grpc_task_submit_and_get` | SubmitTask via gRPC, then GetTask returns same task |
| `grpc_task_list` | Submit 2 tasks, ListTasks returns both |
| `grpc_task_pause_resume` | Submit → Pause → Resume cycle works |
| `grpc_task_watch_stream` | SubmitTask, WatchTask receives events |

### TypeScript (manual / future)

| Test | Assertion |
|------|-----------|
| Client connects | `useGrpcClient` sets `isConnected=true` |
| Client reconnects | After server restart, client auto-reconnects |
| WatchTask stream | `useTaskEvents` receives events and updates subtask state |
| Offline fallback | When server unavailable, TUI shows "Connecting..." and uses offline mode |

---

## 7. Wrong vs Correct

### Wrong: Proto field access with snake_case in JS

```typescript
// Wrong — proto-loader with keepCase:false converts to camelCase
const taskId = event.task_id;
const subtaskId = event.subtask_id;
```

#### Correct

```typescript
// Correct — camelCase after proto-loader transformation
const taskId = event.taskId;
const subtaskId = event.subtaskId;
```

### Wrong: Storing gRPC client in useRef without triggering re-render

```typescript
// Wrong — ref update doesn't cause re-render, downstream hooks miss the change
const clientRef = useRef<TaskServiceClient | null>(null);
// ... connect() sets clientRef.current = newClient
// ... return { client: clientRef.current } // stale!
```

#### Correct

```typescript
// Correct — useState for the value that consumers depend on, useRef for cleanup ref
const [client, setClient] = useState<TaskServiceClient | null>(null);
const clientRef = useRef<TaskServiceClient | null>(null);
// ... connect() sets both: setClient(newClient); clientRef.current = newClient;
// ... useEffect cleanup: clientRef.current?.close()
```

### Wrong: Reloading proto definition on every RPC call

```typescript
// Wrong — disk I/O + parsing on every method call
async submitTask(req) {
    const def = await loadProtoDefinition(this.protoPath); // expensive!
    const client = new def.ultimate_coders.TaskService(this.address, ...);
    return client.SubmitTask(req);
}
```

#### Correct

```typescript
// Correct — load once in constructor, reuse client instance
constructor(address: string) {
    const def = grpc.loadPackageDefinition(packageDef);
    this.client = new def.ultimate_coders.TaskService(address, credentials);
}
async submitTask(req) {
    return new Promise((resolve, reject) => {
        this.client.submitTask(req, (err, resp) => { ... });
    });
}
```

---

## Design Decisions

### Decision: In-memory TaskStore instead of Python Orchestrator

**Context**: The Rust core has no Orchestrator — the real Orchestrator is Python. The gRPC server needs to serve TaskService without depending on Python.

**Options**:
1. Call Python Orchestrator via NATS — requires NATS infrastructure
2. Implement a minimal Rust Orchestrator — significant effort, duplicates Python logic
3. In-memory TaskStore with simple decomposition — sufficient for TUI bridge

**Decision**: Option 3 (in-memory TaskStore). The store uses `Arc<Mutex<HashMap>>` for thread-safe access. Decomposition uses a simple newline-split heuristic. This is explicitly a bridge implementation — when the full Python Orchestrator integration is ready, TaskStore will be replaced.

**Limitation**: Subtask-level events don't carry `task_id` in `AgentEventType`, so `WatchTask` with a specific `task_id` may miss subtask events. Current TUI watches all tasks (empty `task_id`), so this is not a user-facing issue. TODO added in code.

### Decision: Dynamic proto loading (proto-loader) vs code generation

**Context**: The Node.js gRPC client needs proto definitions. Two approaches exist.

**Options**:
1. `@grpc/proto-loader` — dynamic loading at runtime, no build step
2. `grpc-tools` code generation — static TypeScript types, requires build step

**Decision**: Option 1 (proto-loader). Simpler setup, no code generation step in the TUI build pipeline. The proto file is loaded at runtime from the workspace path. TypeScript types are maintained manually in `tui/src/grpc/types.ts`.

### Decision: WatchTask polling (500ms) vs push-based streaming

**Context**: The `EventStore` trait doesn't have a `watch`/`subscribe` method — only `read_from(offset)`.

**Options**:
1. Poll `read_events_from` every 500ms — simple, works with existing EventStore
2. Add a `tokio::sync::watch` channel to EventStore — requires modifying uc-engine
3. Use NATS JetStream consumer — requires NATS infrastructure

**Decision**: Option 1 (polling). Uses `async_stream::stream!` with `tokio::time::sleep(Duration::from_millis(500))`. Simple, no changes to uc-engine, acceptable latency for TUI updates. Can be optimized later with watch channels.

---

## Common Mistakes

1. **Accessing proto fields with snake_case in JavaScript** — `@grpc/proto-loader` with `keepCase: false` (default) converts field names to camelCase. Use `event.taskId` not `event.task_id`.

2. **Storing gRPC client in useRef for React state** — `useRef` updates don't trigger re-renders. Use `useState` for the client value that hooks depend on, and `useRef` only for the cleanup reference.

3. **Not closing gRPC client on unmount** — The `useGrpcClient` hook must close the client channel in the `useEffect` cleanup function, otherwise the gRPC connection leaks.

4. **Hardcoding absolute proto paths** — The proto path must be resolved relative to the package or via `GRPC_PROTO_PATH` env var. Never hardcode `/Users/...` paths.

5. **Pausing/resuming without state validation** — Only `InProgress`/`Planning` tasks can be paused, only `Paused` tasks can be resumed. Always validate before mutating state.
