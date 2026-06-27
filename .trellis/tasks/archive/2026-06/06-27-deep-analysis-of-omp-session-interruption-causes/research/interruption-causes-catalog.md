# Research: Comprehensive Catalog of OMP Session Interruption Causes

- **Query**: All possible causes of OMP session interruption across all layers
- **Scope**: Internal (synthesis)
- **Date**: 2026-06-27

## Findings

### Layer 1: OMP Framework (Agent Process)

| Cause | Mechanism | Symptom | Evidence |
|---|---|---|---|
| LLM stream timeout | `streamFirstEventTimeoutMs` (100s default) or `streamIdleTimeoutMs` exceeded | Agent stops mid-response with error | `packages/ai/src/types.ts:302-324` |
| LLM stream disconnect | SSE stream ends without terminal event | "Proxy stream ended without a terminal event" error | `packages/agent/src/proxy.ts:178` |
| Unexpected LLM stop | `stopReason: "stop"` but output appears truncated | Agent produces incomplete response, classified as unexpected | `packages/coding-agent/src/session/unexpected-stop-classifier.ts:32-42` |
| API rate limit / auth failure | HTTP 429, 401, 403 from provider | Stream error event with rate-limit or auth error code | `packages/ai/src/errors.ts` |
| MCP tool connection error | ECONNREFUSED/ECONNRESET on MCP server | Single automatic reconnect attempt, then tool failure | `packages/coding-agent/src/mcp/tool-bridge.ts:32-50` |
| Worker subprocess death | Parent kills child, child detects disconnect | Child self-terminates with SIGKILL | `packages/coding-agent/src/cli.ts:205-208` |
| RPC stdin close | RPC host disconnects (crash, SIGKILL) | Server calls `process.exit(0)`, rejects pending tool calls | `packages/coding-agent/src/modes/rpc/rpc-mode.ts:1157-1162` |
| RPC command timeout | 30s default timeout on `#send()` | Client rejects with timeout error | `packages/coding-agent/src/modes/rpc/rpc-client.ts:873-913` |

### Layer 2: UC Orchestrator Extension

| Cause | Mechanism | Symptom | Evidence |
|---|---|---|---|
| gRPC server unreachable | `GrpcBridge` methods catch all errors, return null/false/[] | All UC tools silently fail; tasks continue with degraded functionality | `packages/uc-orchestrator/src/orchestrator/grpc-bridge.ts` |
| gRPC server restart | Transport is stale after server restarts via health_monitor | `connected=true` but all calls fail silently; TaskStore on server is empty | `packages/uc-orchestrator/src/orchestrator/grpc-bridge.ts:115-121` |
| No cleanup on session_shutdown | `ControlSignalSubscriber.stop()` never called | Polling timer continues running across sessions; NATS connection leaked | `packages/uc-orchestrator/src/extension.ts:86-88` |
| Event handler accumulation | `session_start` fires without `session_shutdown` clearing | 12+ new handlers per session switch; old orchestrator state persists | `packages/uc-orchestrator/src/extension.ts:68-84` |
| Task map unbounded growth | Completed/failed tasks never evicted | Memory grows linearly with task count | `packages/uc-orchestrator/src/orchestrator/orchestrator.ts:112` |
| Circuit breaker tripped | Too many consecutive subtask failures | "Circuit breaker open" error, all subsequent subtasks fail immediately | `packages/uc-orchestrator/src/orchestrator/orchestrator.ts:526-537` |
| Worker unavailable | All workers offline or overloaded | "No workers available" error, task fails | `packages/uc-orchestrator/src/orchestrator/orchestrator.ts:349-359` |
| Subtask timeout | 10-minute default per subtask | Subtask marked as failed, triggers retry logic | `packages/uc-orchestrator/src/orchestrator/orchestrator.ts:1050` |

### Layer 3: gRPC Server

| Cause | Mechanism | Symptom | Evidence |
|---|---|---|---|
| Server crash + restart | In-memory TaskStore lost | All tasks disappear from gRPC perspective; UC Orchestrator still has local state | `crates/uc-grpc-server/src/main.rs:72-192` |
| NATS subscriber exit | Subscription stream ends (NATS disconnect) | No more task updates received from Python Orchestrator | `crates/uc-grpc/src/server.rs:1412-1622` |
| Heartbeat timeout | No Python consumer heartbeat for 600s | All InProgress/Planning tasks marked Failed | `crates/uc-grpc/src/server.rs:1628-1668` |
| Worker heartbeat timeout | Per-worker heartbeat older than 600s | Subtasks reassigned to Pending and re-dispatched | `crates/uc-grpc/src/server.rs:839-882` |
| Dedup map overflow | >10K seen messages | Old entries purged; potential duplicate processing | `crates/uc-grpc/src/server.rs:298-325` |

### Layer 4: run-omp.sh Process Management

| Cause | Mechanism | Symptom | Evidence |
|---|---|---|---|
| gRPC server crash | Process dies (OOM, panic, signal) | Health monitor detects, restarts with `cargo run` | `run-omp.sh:121-146` |
| Slow restart | `cargo run` compiles before running | 10s port-readiness timeout may expire; OMP continues without gRPC | `run-omp.sh:132-143` |
| OMP process killed | SIGKILL on OMP process | Cleanup trap fires, kills gRPC server, reaps zombies | `run-omp.sh:106-119` |
| Zombie accumulation | gRPC server children not reaped | SIGCHLD handler `reap_children` prevents this | `run-omp.sh:99-104` |
| No notification on restart | Health monitor restarts server but doesn't tell OMP/UC | UC Orchestrator's GrpcBridge transport remains stale | `run-omp.sh:121-146` |

### Layer 5: NATS/Python Worker

| Cause | Mechanism | Symptom | Evidence |
|---|---|---|---|
| NATS server down | Connection refused or dropped | Python worker cannot connect; Rust subscriber exits | `python/ultimate_coders/nats_worker.py:663-696` |
| NATS message loss | Publish while no subscriber | Fire-and-forget publishes silently fail | `python/ultimate_coders/nats_worker.py:239-245` |
| Python worker crash | Unhandled exception, OOM | Heartbeats stop; after 600s Rust marks tasks Failed | `python/ultimate_coders/nats_worker.py:976-1011` |
| Remote worker death | Worker disappears without graceful shutdown | After 90s (Python) or 600s (Rust), subtasks reassigned | `python/ultimate_coders/nats_worker.py:1250-1328` |
| Dispatch deadlock | All subtasks blocked by file conflicts | 30s safety timeout in `_execute_subtasks` prevents permanent hang | `python/ultimate_coders/nats_worker.py:830-838` |
| JetStream unavailable | Stream/consumer creation fails | Non-fatal: no event replay on restart, live events still work | `python/ultimate_coders/nats_worker.py:434-496` |

### Layer 6: Resource Exhaustion

| Cause | Mechanism | Symptom | Evidence |
|---|---|---|---|
| Memory pressure | Unbounded task map + session entries | OOM kill, process restart, all state lost | Analysis of orchestrator.ts, session-manager.ts |
| Event listener leak | session_start without session_shutdown | Growing handler count, slower event dispatch | extension.ts:68-84 |
| FD leak | Stale GrpcBridge transport, unclosed writers | "too many open files" error | grpc-bridge.ts, session-manager.ts writer lifecycle |
| Timer leak | ControlSignalSubscriber pollTimer not stopped | CPU waste, unnecessary gRPC polling | control-signal-subscriber.ts:182, extension.ts:86-88 |

## Critical Chain: gRPC Server Restart

The most impactful interruption chain:

1. gRPC server crashes (any cause)
2. `run-omp.sh` health_monitor detects, restarts server
3. New server has empty in-memory TaskStore
4. UC Orchestrator's `GrpcBridge` still has `connected=true` but stale transport
5. All UC tool calls silently fail (caught and return null/false/[])
6. Active tasks continue locally but cannot sync to gRPC
7. If Python worker was connected via NATS, it continues independently
8. Heartbeat monitor on new server has no heartbeats yet (fresh start)
9. After 600s, new server marks any tasks submitted before restart as Failed (if heartbeats were flowing, they start fresh)

**No recovery path exists** -- the UC Orchestrator never detects that its transport is stale, and there is no mechanism to refresh the `GrpcBridge` transport or re-sync task state.

## Related Specs

- `.trellis/spec/backend/` -- gRPC server configuration and task management specs
