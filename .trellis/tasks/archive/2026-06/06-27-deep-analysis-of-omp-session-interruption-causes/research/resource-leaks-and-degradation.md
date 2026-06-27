# Research: Resource Leaks and Long-Running Session Degradation

- **Query**: Memory leaks, event listener accumulation, file descriptor leaks, timer leaks in long-running OMP sessions
- **Scope**: Internal
- **Date**: 2026-06-27

## Findings

### Files Found

| File Path | Description |
|---|---|
| `packages/uc-orchestrator/src/extension.ts` | Event handler registration without cleanup |
| `packages/uc-orchestrator/src/orchestrator/events.ts` | Event emitter with clear() but no per-handler cleanup |
| `packages/uc-orchestrator/src/orchestrator/orchestrator.ts` | Task/abort controller accumulation, no eviction |
| `packages/uc-orchestrator/src/orchestrator/control-signal-subscriber.ts` | setInterval without cleanup on session_shutdown |
| `packages/uc-orchestrator/src/orchestrator/grpc-bridge.ts` | Single transport, no connection pool or refresh |
| `vendor/oh-my-pi/packages/coding-agent/src/modes/rpc/rpc-client.ts` | Pending request map, event listener arrays |
| `vendor/oh-my-pi/packages/coding-agent/src/session/session-manager.ts` | Session entry accumulation, writer lifecycle |

### Code Patterns

#### 1. Event Listener Accumulation

**UC Orchestrator** (extension.ts):
- On `session_start`, 12 event types are registered via `orchestrator.events.on(type, handler)`
- The unsubscribe functions returned by `.on()` are never stored or called
- On `session_shutdown`, `orchestrator.events.clear()` removes all handlers
- **Issue**: If `session_start` fires multiple times without an intervening `session_shutdown` (e.g., session switch), handlers accumulate. Each `session_start` adds 12 new handlers, but old ones remain until `clear()` is called

**RPC Client** (rpc-client.ts):
- `#eventListeners: RpcEventListener[]` -- array that grows with `onEvent()` calls
- `#sessionEventListeners: RpcSessionEventListener[]` -- same pattern
- `#subagentLifecycleListeners: Set<...>` -- uses Set (deduplication by reference)
- Unsubscribe functions splice from arrays (O(n) per unsubscribe)
- **No limit**: No maximum listener count. In a long-running host process, listeners can accumulate if not properly cleaned up

#### 2. Task and AbortController Accumulation

**UC Orchestrator** (orchestrator.ts):
- `tasks: Map<string, TaskState>` -- tasks are never evicted. Completed/failed/cancelled tasks remain in memory indefinitely
- `abortControllers: Map<string, AbortController>` -- only cleaned up implicitly (no explicit delete)
- `progressState: Map<string, ProgressWidgetState>` (in extension.ts) -- cleaned up on `task_complete` but not on `task_failed`
- **No task eviction**: There is no TTL, LRU, or max-count limit on the tasks Map. Over a long session with many tasks, this grows unboundedly

#### 3. Timer Leaks

**ControlSignalSubscriber** (control-signal-subscriber.ts):
- `pollTimer: ReturnType<typeof setInterval>` -- started in `start()`, cleaned up in `stop()`
- **But `stop()` is never called from `extension.ts`'s `session_shutdown` hook**
- If NATS is unavailable, the polling timer runs every 2s indefinitely, creating a new `GrpcBridge` instance each interval

**Orchestrator** (orchestrator.ts):
- `setTimeout(r, 5000)` for worker availability retry (line 352) -- not stored, not cancellable
- `setTimeout(resolve, 100)` for file conflict wait (line 516) -- not stored
- `setTimeout(resolve, delay)` for retry backoff (line 993) -- not stored
- These are one-shot timers that resolve naturally, but they are not tracked for cleanup on session shutdown

#### 4. Session Entry Growth

**SessionManager** (session-manager.ts):
- `#entries: SessionEntry[]` -- append-only array. Compaction can reduce entries, but compaction is triggered by token budget, not entry count
- `#index: SessionEntryIndex` -- maintains `#entriesById` Map, `#children` Map, `#labels` Map. All grow with entries
- `#diskTail: Promise<void>` -- serialized disk work chain. Never explicitly cleared except on `#resetToNewSession`
- **Writer lifecycle**: `#writer` is opened lazily and closed on `close()`, `flush()`, or `#drainAndCloseWriter()`. If the process crashes without flush, the writer handle may leak (OS-level FD leak)

#### 5. GrpcBridge Transport Staleness

**GrpcBridge** (grpc-bridge.ts):
- Single `createGrpcWebTransport()` created at construction
- Transport wraps an HTTP/2 connection to `http://localhost:50051`
- If the gRPC server restarts (run-omp.sh health_monitor), the old HTTP/2 connection becomes stale
- **No transport refresh**: The bridge has no method to recreate the transport. The `connected` flag can be `true` while the underlying connection is dead
- **No connection pool**: Unlike a typical HTTP client with connection pooling and keep-alive, the connectrpc transport manages a single connection

#### 6. Memory Pressure Indicators

Potential memory pressure sources in long-running sessions:

1. **Task Map**: Each `TaskState` contains subtask arrays with result strings (up to 2000 chars each). With many tasks, this can grow significantly
2. **Session entries**: Before compaction, all messages (including tool results with large outputs) are held in memory
3. **Event dedup maps**: `seenMessageIds` (10K max), `seen_messages` (10K max with 5-min TTL) -- bounded but contribute to baseline
4. **Orchestrator event handlers**: If session_start fires without session_shutdown, handler count grows linearly
5. **Progress widget state**: `progressState` Map in extension.ts -- one entry per active task, cleaned up on completion

### Caveats / Not Found

- No explicit memory monitoring or pressure detection in the codebase
- No `process.memoryUsage()` checks or GC hints
- The OMP agent has auto-compaction (triggered by token budget), which reduces session entry memory, but the UC Orchestrator's task state is outside OMP's compaction scope
- No maximum task count or TTL configuration for the UC Orchestrator
- The `GrpcBridge.connected` flag is unreliable: it's set to `true` on successful `health()` but never explicitly set to `false` on connection errors
