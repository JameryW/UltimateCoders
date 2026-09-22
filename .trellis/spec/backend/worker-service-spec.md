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
    string contract_version = 5; // T1 #637 handshake (empty = legacy worker)
}

message RegisterWorkerResponse {
    bool success = 1;
    string worker_id = 2;
    optional string error = 3;
}

message WorkerHeartbeatRequest {
    string worker_id = 1;
    uint32 current_load = 2;
    string contract_version = 3; // re-asserted each heartbeat (T1 #637)
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
    pub contract_version: String,
    pub registered_at: chrono::DateTime<chrono::Utc>,
    pub last_heartbeat: chrono::DateTime<chrono::Utc>,
}

impl WorkerRegistry {
    pub fn new() -> Self
    pub fn register(&mut self, worker_id, capabilities, max_capacity, metadata, contract_version) -> Result<(), String>
    pub fn register_with_projects(&mut self, worker_id, capabilities, max_capacity, metadata, contract_version, projects) -> Result<(), String>
    pub fn heartbeat(&mut self, worker_id, current_load) -> Result<(), String>
    pub fn heartbeat_with_signals(&mut self, worker_id, current_load, recent_files, per_worker_topic) -> Result<(), String>
    pub fn deregister(&mut self, worker_id) -> Result<(), String>
    pub fn workers(&self) -> &HashMap<String, RegisteredWorker>
    pub fn available_workers(&self) -> Vec<&RegisteredWorker>
    pub fn workers_with_capabilities(&self, required: &[String]) -> Vec<&RegisteredWorker>
    pub fn workers_with_capabilities_excluding(&self, required: &[String], exclude: &HashSet<String>) -> Vec<&RegisteredWorker>
    pub fn dispatchable_workers_with_capabilities(&self, required: &[String]) -> Vec<&RegisteredWorker>
    pub fn dispatch_gate(&self, required: &[String], project_id: &str, independence: &ReviewIndependence) -> WorkerDispatchGate
    pub fn dispatch_candidates(&self, required: &[String], project_id: &str, exclude: &HashSet<String>) -> Vec<&RegisteredWorker>
    pub fn placement_target(&self, required: &[String], project_id: &str, file_constraints: &[String], sibling_hosts: &HashSet<String>, exclude: &HashSet<String>) -> Option<placement::Placement>
    pub fn to_worker_protos(&self) -> Vec<WorkerProto>
}
```

> `workers_with_capabilities_excluding` is the **single** place the capability
> roster is computed (T19 #670). `dispatch_gate` / `dispatch_candidates` /
> `placement_target` all descend from it, which is what makes the T12 invariant
> — *scoring must never see a worker the gate would reject* — hold structurally
> rather than by convention. `exclude` is a **required** parameter on both
> dispatch mouths: an optional one would let a future dispatch path silently
> re-open self-review by omitting it.

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

**Metadata contract (cross-host observability)**: workers send a compact
JSON blob in `RegisterWorkerRequest.metadata`; the registry stores it
verbatim and `ListWorkers` echoes it in `WorkerProto.metadata`.
Stable keys: `hostname` + `pid` always present; `compose_project` only
when running under a compose project (`UC_COMPOSE_PROJECT`);
`contract_version` always present (T1 #637 echo — the authoritative value
is the explicit `RegisterWorkerRequest.contract_version` proto field, not
this key). Empty when the worker sends nothing. Consumers MUST treat it as
opaque JSON with best-effort parsing.

**Contract-version handshake (T1 #637, hard gate)**:
- `uc_types::CONTRACT_VERSION` (Rust) == `nats_worker.CONTRACT_VERSION`
  (Python) — bumped only together; AGENTS.md documents the lockstep order
  (gateway first, then workers).
- Register/heartbeat with a non-empty mismatched version → refused
  (`success=false` / `accepted=false` + `tracing::warn`); worker never
  enters the registry.
- Empty version (legacy worker) → accepted (observability) but
  **never dispatchable**: `publish_ready_subtasks` /
  `dispatch_ready_subtasks` consult `WorkerRegistry::dispatch_gate` and
  keep nodes `Pending` + `tracing::warn` while no capability-matching
  available worker declares `CONTRACT_VERSION`. An entirely empty
  registry with no capability requirements preserves the best-effort
  NATS-only publish.

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

**Scope hard filter (T8 #650)**: a worker registered with `projects` only serves
those project_ids; a node whose `project_id` matches none of them is kept
`Pending` (`NoScopeMatchedWorker`). An **empty** task scope is served only by
unscoped ("open") workers.

**Review independence (T19 #670, hard gate)**: a node is a *review node* iff its
`required_capabilities` contains `"review"` — the same predicate behind the
`review` graph label (`uc_engine::requires_independence`, called by
`node_type_for`; never re-derive it). For such a node the producers of its
dependencies (`Subtask::assigned_worker`) are **excluded** from the candidate
roster, and dispatch fails closed in two distinguishable ways:

| Condition | Verdict | Counter |
|-----------|---------|---------|
| a dependency's producer is unknown (field absent, or dependency missing from the snapshot) | `ProducerIdentityUnknown { dependencies }` | `producer_identity_unknown_count()` |
| the producers were identified and they were the only capability-holders | `NoIndependentReviewer { producers }` | `no_independent_reviewer_count()` |

Both leave the node `Pending` + `tracing::warn`. The two counters are
deliberately **not** merged: "add a reviewer worker" and "fix the producer's
reporting" are different fixes. The checks run *before* the scope/version
filters so the verdict names the actual problem.

⚠️ **Known limit — the gate is a roster check, not a delivery guarantee.**
Delivery is a shared durable work-queue; `resolve_dispatch_subject` falls back to
the shared subject whenever affinity placement returns `None` (the normal case —
affinity is a soft preference, D12). So when ≥2 candidates remain and one of them
is the producer, the work-queue may still deliver the review to it. The gate only
removes the *worst* shape (the producer as the sole candidate). Closing the
window needs per-worker subjects everywhere **plus** promoting affinity from
preference to gate — rejected as structurally impossible for legacy workers
(no per-worker subject) and as colliding with D12. Opt-in
`UC_PLACEMENT_POLICY=capacity` changes ranking only
([runtime-policy-spec.md](./runtime-policy-spec.md)); it does not make this
roster check a delivery guarantee.

⚠️ **`"review"` is not a default worker capability** (`UC_CAP_REVIEW` opts in,
`worker.py:498`), so the fail-closed paths are reachable only in clusters that
actually contain a reviewer-capable worker. Ordinary (`NoCapableWorker`) refusal
still comes first for the rest.

### Stale Worker Detection

- `STALE_TIMEOUT_SECS = 60.0` — workers not heartbeating within 60s are considered unavailable
- `RegisteredWorker.is_available()` checks heartbeat age
- Dashboard `ListWorkers` falls back to WorkerRegistry when NATS passthrough unavailable

---

## 4. Validation & Error Matrix

| Condition | Behavior |
|-----------|----------|
| Empty worker_id in RegisterWorker | Returns `success=false, error="worker_id cannot be empty"` |
| Non-empty `contract_version` ≠ gateway `CONTRACT_VERSION` (register OR heartbeat) | Refused: `success=false` / `accepted=false`, error names both versions; registry untouched |
| Empty `contract_version` | Accepted (legacy) but excluded from dispatch gate — subtasks stay Pending + `tracing::warn` until a version-matched worker registers |
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

## 6b. ScaleWorkers "scale" action (cross-host fan-out)

- **Hosts source**: `UC_SCALE_HOSTS` — comma/semicolon-separated docker
  connection specs. `local` (case-insensitive) = the daemon the gateway
  itself reaches (no `DOCKER_HOST` override); any other spec is passed as
  `DOCKER_HOST` for that invocation (`ssh://user@host` works without
  pre-registered docker contexts). Unset/empty/blank → `["local"]`
  (byte-for-byte single-host behavior).
- **Split**: `split_target_across_hosts(target, hosts)` — even split,
  first `target % n` hosts take one extra worker; order preserved.
  `target=0` scales every host down to zero.
- **Per-host execution**: one
  `docker compose -p <proj> -f <file> up -d --no-deps --scale worker=<share> worker`
  per host (`--no-deps` MANDATORY — the gateway depends_on itself would
  deadlock). Best-effort: one unreachable host does not block others.
- **Aggregation**: `success=true` only when ALL hosts succeed;
  `actual_count` = sum of shares on successful hosts; failures joined in
  `error`, per-host detail (`host=share` / `host=FAILED(...)`) in `message`.
- **Dry-run plan mode**: `UC_SCALE_DRY_RUN` truthy (`1/true/yes/on`,
  case-insensitive) → the scale action returns `success=true` with
  `actual_count = target` (PLANNED count) and a `DRY-RUN: …` message
  listing per-host `host=share` pairs; NO docker invocation and the
  compose-file pre-check is skipped (plan must be inspectable on
  gateways without a local compose file). Anything else → executes.
- **Remote-worker prerequisites** (deployment config, not gateway code):
  reachable gateway address + external git sync (`UC_REPO_URL`).

---

## 7. Environment Variables

| Key | Required | Default | Purpose |
|-----|----------|---------|---------|
| `UC_GRPC_ENDPOINT` | No | — | gRPC server address for WorkerService registration (e.g., `http://localhost:50051`) |
| `UC_GRPC_ADDR` | No | `[::]:50051` | gRPC server listen address (Rust server side) |
| `UC_SCALE_HOSTS` | No | _(unset = `local`)_ | Comma/semicolon list of docker connection specs for cross-host ScaleWorkers fan-out |
| `UC_PLACEMENT_POLICY` | No | `affinity` | `affinity` or `capacity`. Any other value, including blank, fails gateway startup. See [runtime-policy-spec.md](./runtime-policy-spec.md) |
| `UC_COMPOSE_FILE` / `UC_COMPOSE_PROJECT` | No | `/app/docker/docker-compose.yml` / `docker` | Compose target for the scale action |
