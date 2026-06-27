# Research: Worker Registration

- **Query**: How do workers register themselves? How does the server know about available workers?
- **Scope**: internal
- **Date**: 2026-06-27

## Findings

### Files Found

| File Path | Description |
|---|---|
| `crates/uc-grpc/src/server.rs` | TaskStore with worker_heartbeats tracking |
| `crates/uc-grpc/src/dashboard_service.rs` | ListWorkers RPC (NATS passthrough + heartbeat fallback) |
| `python/ultimate_coders/nats_worker.py` | NatsWorker heartbeat publishing + remote worker discovery |
| `python/ultimate_coders/agent/orchestrator.py` | Python Orchestrator register_worker + refresh_heartbeat |
| `python/ultimate_coders/agent/worker.py` | Worker.get_info() + send_heartbeat() |

### Registration Model

There is **no explicit registration RPC or message**. Workers are discovered and tracked through heartbeats on the `uc.heartbeat` NATS subject. This is an implicit registration model.

### How the Rust gRPC Server Learns About Workers

The gRPC server's `TaskStore` maintains a `worker_heartbeats: HashMap<String, DateTime<Utc>>` map (line 234). Workers are added to this map when the NATS subscriber receives a heartbeat:

```
heartbeat_sub -> parse NatsHeartbeat -> store.update_worker_heartbeat(consumer_id)
```

At line 1641-1647 in `spawn_nats_subscriber`:
```rust
Some(message) = heartbeat_sub.next() => {
    let mut store = task_store.lock().await;
    store.update_last_heartbeat();
    if let Ok(hb) = serde_json::from_slice::<NatsHeartbeat>(&message.payload) {
        store.update_worker_heartbeat(&hb.consumer_id);
    }
}
```

### How the Python Orchestrator Learns About Workers

Two mechanisms:

1. **Direct registration**: NatsWorker calls `orchestrator.register_worker(worker_info)` during `_init_components()` (line 655). This adds a `WorkerEntry` to `orchestrator.workers` dict with `id`, `capabilities`, `max_capacity`.

2. **Remote worker discovery via heartbeats**: NatsWorker subscribes to `uc.heartbeat` in default mode (line 347-352). When it receives a heartbeat from another worker, it updates `_known_remote_workers` dict (line 1230) and calls `orchestrator.refresh_heartbeat(worker_id)`.

### Worker Heartbeat Payload

Published by NatsPublisher every 30 seconds via `_heartbeat_loop()` (line 976):

```json
{
    "consumer_id": "uuid",
    "timestamp": "2026-06-27T12:00:00Z",
    "worker_id": "uuid",
    "capabilities": ["code", "search", "memory", "test"],
    "current_load": 0,
    "max_capacity": 3,
    "pending_subtask_count": 5
}
```

The Rust side parses only `consumer_id` for `update_worker_heartbeat()` (line 1645). The extended worker info (capabilities, load, capacity) is used by the Python Orchestrator for scheduling.

### ListWorkers RPC

`DashboardService::list_workers` (dashboard_service.rs line 38):
1. **Primary path**: NATS request-reply to `uc.dashboard.ListWorkers` -> Python Orchestrator returns full worker info from `orchestrator.workers` dict
2. **Fallback path** (NATS unavailable): Constructs WorkerProto list from `TaskStore::worker_heartbeats()` map with default capabilities `["code"]`, default max_capacity=3, and availability based on heartbeat age (stale if >60s)

### Worker Availability Check

The gRPC server determines worker availability in two ways:
1. **TaskStore worker_heartbeats**: Heartbeat age < timeout means worker is available
2. **Dashboard NATS passthrough**: Queries Python Orchestrator which has richer state (current_load, is_available)

The OMP orchestrator uses `bridge.listWorkers()` to check availability before executing each wave (orchestrator.ts line 331-351).

### Stale Worker Detection and Cleanup

**Rust side** (`spawn_heartbeat_monitor`, server.rs line 1661):
- Runs every 30 seconds
- `mark_stale_workers(heartbeat_timeout)` removes workers older than timeout from `worker_heartbeats`
- `reassign_stale_subtasks()` resets their subtasks to Pending
- Re-dispatches reassigned subtasks via `dispatch_ready_subtasks()`

**Python side** (`_stale_worker_cleanup_loop`, nats_worker.py line 1250):
- Runs every 60 seconds
- Removes remote workers with no heartbeat for >90s
- Reassigns their in-progress subtasks back to Pending with incremented retry_count
- Publishes `subtask_retrying` event for visibility
- Also detects local worker heartbeat stalls (line 1301)

## Caveats / Not Found

- The Rust gRPC server's `worker_heartbeats` map uses `consumer_id` as the key (from `NatsHeartbeat.consumer_id`), NOT `worker_id`. The `NatsHeartbeat` struct (line 150-154) only has `consumer_id` and `timestamp` -- the worker_id, capabilities, and load info in the JSON payload are ignored by the Rust parser.
- The Python `_handle_heartbeat()` method (line 1214) uses `worker_id` from the extended heartbeat payload (not `consumer_id`) as the key for `_known_remote_workers`.
- There is a potential naming inconsistency: the Rust heartbeat uses `consumer_id` while Python heartbeat discovery uses `worker_id`.
- Workers do NOT register capabilities with the Rust gRPC server -- the server only knows they exist via heartbeats. Capability-aware scheduling only happens in the Python Orchestrator.
