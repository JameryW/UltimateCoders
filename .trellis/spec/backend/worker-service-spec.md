# WorkerService gRPC Spec

> Executable contracts for Worker registration, heartbeat, and deregistration via gRPC WorkerService.

---

## 1. Scope / Trigger

- Trigger: Worker lifecycle management — startup registration, periodic heartbeat, graceful shutdown
- Cross-layer: Python Worker → gRPC WorkerService → Rust WorkerRegistry → Dashboard ListWorkers
- Replaces: NATS heartbeat passive discovery (uc.heartbeat) with active gRPC registration

---

## 2. Signatures

### Proto (engine.proto)

```protobuf
service WorkerService {
    rpc RegisterWorker(RegisterWorkerRequest) returns (RegisterWorkerResponse);
    rpc WorkerHeartbeat(WorkerHeartbeatRequest) returns (WorkerHeartbeatResponse);
    rpc DeregisterWorker(DeregisterWorkerRequest) returns (DeregisterWorkerResponse);
}

message RegisterWorkerRequest {
    string worker_id = 1;
    repeated string capabilities = 2;
    uint32 max_capacity = 3;
    string metadata = 4;        // optional JSON metadata
}

message RegisterWorkerResponse {
    bool success = 1;
    string worker_id = 2;
    optional string error = 3;
}

message WorkerHeartbeatRequest {
    string worker_id = 1;
    uint32 current_load = 2;
}

message WorkerHeartbeatResponse {
    bool accepted = 1;
    optional string error = 2;
}

message DeregisterWorkerRequest {
    string worker_id = 1;
}

message DeregisterWorkerResponse {
    bool success = 1;
    optional string error = 2;
}
```

### Rust WorkerRegistry (`crates/uc-grpc/src/worker_service.rs`)

```rust
pub struct WorkerRegistry {
    workers: HashMap<String, RegisteredWorker>,
}

pub struct RegisteredWorker {
    pub id: String,
    pub capabilities: Vec<String>,
    pub max_capacity: u32,
    pub current_load: u32,
    pub metadata: String,
    pub registered_at: chrono::DateTime<chrono::Utc>,
    pub last_heartbeat: chrono::DateTime<chrono::Utc>,
}

impl WorkerRegistry {
    pub fn new() -> Self
    pub fn register(&mut self, worker_id, capabilities, max_capacity, metadata) -> Result<(), String>
    pub fn heartbeat(&mut self, worker_id, current_load) -> Result<(), String>
    pub fn deregister(&mut self, worker_id) -> Result<(), String>
    pub fn workers(&self) -> &HashMap<String, RegisteredWorker>
    pub fn available_workers(&self) -> Vec<&RegisteredWorker>
    pub fn workers_with_capabilities(&self, required: &[String]) -> Vec<&RegisteredWorker>
    pub fn to_worker_protos(&self) -> Vec<WorkerProto>
}
```

### Python Engine (`python/ultimate_coders/engine.py`)

```python
class Engine:
    async def register_worker_async(self, worker_id, capabilities, max_capacity) -> bool
    async def worker_heartbeat_async(self, worker_id, current_load) -> bool
    async def deregister_worker_async(self, worker_id) -> bool
```

---

## 3. Contracts

### Registration Flow

1. Worker starts → connects to NATS → initializes components
2. If `UC_GRPC_ENDPOINT` is set → creates gRPC Engine → calls `register_worker_async`
3. On success → worker_id + capabilities registered in WorkerRegistry
4. On failure → non-fatal, worker operates in NATS-only mode

### Heartbeat Flow

1. NATS heartbeat loop (every 30s) also sends gRPC `WorkerHeartbeat`
2. Updates `current_load` in WorkerRegistry
3. Failure → logged at debug level, non-fatal

### Deregistration Flow

1. Worker.stop() → calls `deregister_worker_async`
2. WorkerRegistry removes the worker entry
3. Failure → non-fatal (best-effort)

### Capability-Aware Dispatch

- `publish_ready_subtasks()` checks `WorkerRegistry.workers_with_capabilities()` before dispatching
- Subtasks with `required_capabilities` that have no matching worker → kept Pending
- Subtasks without `required_capabilities` → dispatched normally via NATS

### Stale Worker Detection

- `STALE_TIMEOUT_SECS = 60.0` — workers not heartbeating within 60s are considered unavailable
- `RegisteredWorker.is_available()` checks heartbeat age
- Dashboard `ListWorkers` falls back to WorkerRegistry when NATS passthrough unavailable

---

## 4. Validation & Error Matrix

| Condition | Behavior |
|-----------|----------|
| Empty worker_id in RegisterWorker | Returns `success=false, error="worker_id cannot be empty"` |
| Duplicate worker_id in RegisterWorker | Re-registers (overwrites previous, resets current_load to 0) |
| Heartbeat for unregistered worker | Returns `accepted=false, error="Worker not registered"` |
| Deregister unknown worker_id | Returns `success=false, error="Worker not found"` |
| gRPC endpoint not configured | Worker operates in NATS-only mode (no registration) |
| gRPC registration fails | Non-fatal, worker continues without gateway registration |
| gRPC heartbeat fails | Non-fatal, logged at debug level |
| gRPC deregistration fails | Non-fatal, logged at warning level |

---

## 5. Good/Base/Bad Cases

- **Good**: Worker starts → RegisterWorker → periodic Heartbeat → Deregister on shutdown → Gateway shows correct state
- **Base**: Worker starts → no gRPC endpoint → NATS-only mode → operates normally
- **Bad**: Worker crashes without Deregister → heartbeat timeout marks it unavailable → subtasks re-dispatched

---

## 6. Tests Required

| Test | Assertion |
|------|-----------|
| `registry_register_and_lookup` | Register + retrieve worker by ID |
| `registry_rejects_empty_id` | Empty worker_id returns error |
| `registry_heartbeat_updates_load` | Heartbeat updates current_load |
| `registry_heartbeat_unknown_worker` | Heartbeat for unknown worker returns error |
| `registry_deregister` | Deregister removes worker |
| `registry_deregister_unknown` | Deregister unknown returns error |
| `registry_workers_with_capabilities` | Filters by required capabilities |
| `registry_to_worker_protos` | Converts to proto WorkerProto list |
| `registry_reregister_resets_state` | Re-register overwrites with new capabilities |

---

## 7. Environment Variables

| Key | Required | Default | Purpose |
|-----|----------|---------|---------|
| `UC_GRPC_ENDPOINT` | No | — | gRPC server address for WorkerService registration (e.g., `http://localhost:50051`) |
| `UC_GRPC_ADDR` | No | `[::]:50051` | gRPC server listen address (Rust server side) |
