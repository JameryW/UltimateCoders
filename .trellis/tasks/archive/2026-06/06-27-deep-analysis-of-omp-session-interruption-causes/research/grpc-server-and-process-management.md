# Research: gRPC Server Connection Management and Health Monitoring

- **Query**: gRPC server connection lifecycle, health checks, timeouts, zombie process handling
- **Scope**: Internal
- **Date**: 2026-06-27

## Findings

### Files Found

| File Path | Description |
|---|---|
| `crates/uc-grpc-server/src/main.rs` | gRPC server binary: tonic + gRPC-Web + CORS, task backend selection |
| `crates/uc-grpc/src/server.rs` | Server implementation: TaskStore, NATS subscriber, heartbeat monitor, dedup |
| `run-omp.sh` | Process orchestration: health monitor, zombie reaping, cleanup traps |

### Code Patterns

#### 1. gRPC Server Architecture

The gRPC server (`main.rs`) uses:
- `tonic::transport::Server` with `accept_http1(true)` for gRPC-Web compatibility
- `GrpcWebLayer` for browser client support
- CORS layer configured by `UC_CORS_MODE` (dev: allow any, production: explicit origins)
- Health service registered via `tonic_health::server::health_reporter()` -- marks `EngineService` as `Serving`
- No explicit timeout configuration on the tonic server (defaults apply: HTTP/2 keep-alive, stream timeout etc.)

The server has no graceful shutdown handler -- it just calls `.serve(addr).await?` and exits when that completes or errors.

#### 2. TaskStore and NATS Integration

From `server.rs`:
- **In-memory TaskStore**: `HashMap<String, Task>` with no persistence across restarts (unless PostgreSQL backend configured)
- **Broadcast channel**: `broadcast::channel(256)` for real-time event streaming to WatchTask clients
- **NATS subscriber**: When NATS connected, spawns background `tokio::spawn` tasks:
  - `spawn_nats_subscriber`: Subscribes to `uc.task.update`, `uc.task.event`, `uc.heartbeat`
  - `spawn_heartbeat_monitor`: Checks every 30s for stale consumer/worker heartbeats
- **Heartbeat timeout**: Default 600s (10 min). If no heartbeat received, marks all InProgress/Planning tasks as Failed
- **Worker-level failover**: `mark_stale_workers()` detects workers with heartbeats older than timeout, reassigns their subtasks to Pending, and re-dispatches
- **Dedup**: `seen_messages` HashMap with 5-minute TTL, max 10K entries. Prevents NATS at-least-once redelivery duplicates

#### 3. NATS Subscriber Error Handling

The NATS subscriber (`spawn_nats_subscriber`) uses `tokio::select!` to multiplex three subscriptions. If ANY subscription fails:
- Initial subscription failure: Logs warning and returns (subscriber not running)
- Stream end (all subscriptions return `None`): Logs "NATS subscription ended, subscriber exiting" and breaks the loop
- Message parse failure: Logs warning and continues

**Critical gap**: If the NATS connection itself drops (network partition, NATS server restart), the `async_nats::Client` does have built-in reconnect logic, but the subscriber loop may exit if the subscription objects become invalid. There is no mechanism to re-spawn the subscriber.

#### 4. run-omp.sh Process Management

From `run-omp.sh`:
- **SIGCHLD handler**: `trap reap_children CHLD` -- prevents zombie accumulation from gRPC server subprocess
- **Cleanup on EXIT/INT/TERM**: `cleanup()` function kills gRPC server PID, reaps zombies, optionally stops Docker
- **Health monitor**: Background function checks every 10s if `SERVER_PID` is alive via `kill -0`. If dead:
  1. Logs "gRPC server died"
  2. Waits for old process
  3. Shows last 10 lines of log
  4. Restarts with `cargo run -p uc-grpc-server`
  5. Updates `SERVER_PID`
  6. Waits up to 10s for port 50051 to be ready

**Critical issues**:
- The health monitor restarts the server but never notifies the OMP process or the UC Orchestrator extension. Their gRPC-Web transports remain stale
- The restarted server has empty TaskStore (in-memory) -- all task state from the previous server instance is lost
- If the gRPC server crashes during an active task's gRPC call, the UC Orchestrator's `GrpcBridge` silently returns `null`/`false`, and the task continues with degraded functionality (no gRPC sync)
- `cargo run -p uc-grpc-server` is slow (compiles if needed) -- 10s port readiness timeout may not be enough

#### 5. Task Persistence Gap

The `TaskStore` is in-memory by default:
- `InMemoryTaskBackend`: HashMap, no persistence. Server restart = all task state lost
- `PostgresTaskBackend`: Requires `UC_TASK_BACKEND=postgres` + `UC_DATABASE_URL`. Available only with `storage` feature flag
- The gRPC server's `create_task_backend()` function warns and falls back to in-memory on PostgreSQL failure

The UC Orchestrator has its own local persistence (`TaskStore` in task-store.ts) which writes JSON files to `.uc-tasks/`. This survives OMP restarts but is separate from the gRPC server's TaskStore.

### Caveats / Not Found

- The gRPC server has no stream timeout configuration -- tonic defaults apply (typically 30s idle timeout for HTTP/2 streams)
- There is no gRPC-Web specific timeout -- the connect-web client's default may differ from the server's
- No mechanism to propagate gRPC server restart events to OMP/UC extension
- The `health()` RPC in `GrpcBridge` (UC Orchestrator) does set `connected = true` on success, but the OMP extension never calls `health()` periodically -- it only relies on the initial connection status
