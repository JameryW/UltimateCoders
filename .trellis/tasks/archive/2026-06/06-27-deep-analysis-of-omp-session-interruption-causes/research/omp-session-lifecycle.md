# Research: OMP Session Lifecycle and Interruption Causes

- **Query**: OMP (oh-my-pi) session lifecycle, interruption conditions, RPC disconnect/timeout/reconnect
- **Scope**: Internal
- **Date**: 2026-06-27

## Findings

### Files Found

| File Path | Description |
|---|---|
| `vendor/oh-my-pi/packages/coding-agent/src/session/session-manager.ts` | Session persistence, JSONL journal, append-only entries, flush/close lifecycle |
| `vendor/oh-my-pi/packages/coding-agent/src/modes/rpc/rpc-client.ts` | RPC client: spawns agent process, JSON stdin/stdout protocol, pending requests, timeouts |
| `vendor/oh-my-pi/packages/coding-agent/src/modes/rpc/rpc-mode.ts` | RPC server mode: reads JSON from stdin, dispatches commands, handles shutdown |
| `vendor/oh-my-pi/packages/coding-agent/src/mcp/tool-bridge.ts` | MCP reconnect logic: isRetriableConnectionError, auto-reconnect on stale connections |
| `vendor/oh-my-pi/packages/coding-agent/src/session/unexpected-stop-classifier.ts` | Classifies unexpected LLM stops (truncated output detection) |
| `vendor/oh-my-pi/packages/agent/src/proxy.ts` | Proxy stream: SSE-based LLM proxy, abort handling, terminal event detection |
| `vendor/oh-my-pi/packages/coding-agent/src/extensibility/shared-events.ts` | session_start / session_shutdown event definitions |
| `vendor/oh-my-pi/packages/coding-agent/src/cli.ts` | Process lifecycle: disconnect handler, SIGKILL on orphan, worker spawn |
| `packages/uc-orchestrator/src/extension.ts` | UC extension entry: session_start/session_shutdown hooks, event wiring |
| `packages/uc-orchestrator/src/orchestrator/events.ts` | OrchestratorEventEmitter with clear() on session_shutdown |
| `packages/uc-orchestrator/src/orchestrator/orchestrator.ts` | Full task orchestration: submit/decompose/execute waves, abort/retry/circuit breaker |
| `packages/uc-orchestrator/src/orchestrator/grpc-bridge.ts` | gRPC-Web bridge: connectrpc transport, timeout config, error swallowing |
| `packages/uc-orchestrator/src/orchestrator/control-signal-subscriber.ts` | NATS control events + polling fallback for pause/resume/cancel |

### Code Patterns

#### 1. OMP Session Lifecycle

The session lifecycle is managed by `SessionManager` (session-manager.ts):

- **Creation**: `SessionManager.create()` or `SessionManager.continueRecent()` mints a new session ID, writes a JSONL header
- **Persistence**: Append-only JSONL journal. Appends are synchronous and software-crash durable (in-body write, no fsync). Full rewrites use atomic temp-write+rename
- **Shutdown**: `flushSync()` called on Ctrl+C to persist in-memory entries. `close()` drains async writer
- **Recovery**: `continueRecent()` uses terminal breadcrumbs (`.pi-last-session`) to find last session; resolves subagent breadcrumb poisoning by walking up to interactive root

Key interruption condition: The session file is lazily created. A session that never produces an assistant message never creates a file. If the process crashes before `flushSync()`, entries appended via the async disk chain may be lost (though in-body synchronous appends survive OOM/SIGKILL).

#### 2. RPC Mode Disconnect Handling

The RPC client (`rpc-client.ts`) communicates with the agent via JSON over stdin/stdout:

- **Startup**: Spawns `bun packages/coding-agent/src/cli.ts --mode rpc`, waits for `{ type: "ready" }` with 30s timeout
- **Process death**: Races `process.exited` against the ready signal. If process exits before ready, rejects with exit code + stderr
- **Request timeouts**: Default 30s per command (`#send`), 60s for `waitForIdle()` / `collectEvents()`
- **Stdin close detection**: In `rpc-mode.ts` line 1117-1162, when `readJsonl(Bun.stdin.stream())` ends (stdin closed), the server calls `process.exit(0)` after rejecting pending host tool calls
- **No reconnect**: The RPC client has NO reconnect mechanism. If the process dies, the client is dead. The caller must create a new `RpcClient` instance

The RPC server (`rpc-mode.ts`) handles:
- `shutdownState.requested`: Extensions can request shutdown via `onShutdown` callback. Checked after each command
- `session_shutdown` event: Extensions are notified, then `process.exit(0)`
- Pending extension requests: `Map<string, PendingExtensionRequest>` is cleaned up on shutdown

#### 3. MCP Reconnect Pattern

`tool-bridge.ts` has the most explicit reconnect logic:

- `isRetriableConnectionError()`: Detects ECONNREFUSED, ECONNRESET, EPIPE, ENETUNREACH, EHOSTUNREACH, "fetch failed", "transport not connected", "transport closed", "network error", plus HTTP 404/502/503
- When a retriable error occurs and a `reconnect` callback exists, it tears down the stale connection, creates a new one, and retries the tool call once
- `MCPReconnect` type: `() => Promise<MCPServerConnection | null>`
- Manual reconnect via `/mcp reconnect <name>` command

#### 4. Stream-Level Interruptions

The AI stream layer has multiple timeout mechanisms:

- `streamFirstEventTimeoutMs`: Time to first event from LLM (default 100s, env `PI_STREAM_FIRST_EVENT_TIMEOUT_MS`)
- `streamIdleTimeoutMs`: Inter-event stall timeout (env `PI_STREAM_IDLE_TIMEOUT_MS`)
- Provider-specific overrides: `PI_OPENAI_STREAM_FIRST_EVENT_TIMEOUT_MS`, `PI_OPENAI_STREAM_IDLE_TIMEOUT_MS`
- Proxy stream (`proxy.ts`): Detects "stream ended without a terminal event" as an error condition. If the signal is aborted, throws AbortError; otherwise throws generic stream error
- `unexpected-stop-classifier.ts`: When LLM output ends with `stopReason: "stop"` but the text looks truncated (no tool calls, has text content), a secondary LLM call classifies whether the stop was unexpected

#### 5. Process-Level Lifecycle

From `cli.ts`:
- **Worker subprocess**: Spawns with `--subprocess` flag. IPC channel via stdin/stdout. Parent sends SIGKILL on shutdown. Child monitors `process.on("disconnect")` and self-terminates with SIGKILL
- **Graceful shutdown**: SIGINT/SIGTERM handlers flush session, then exit. The `quit()` function awaits registered cleanups before `process.exit()`
- **Orphan detection**: If parent crashes/SIGKILL, the `disconnect` event fires on the child, which then calls `shutdown()` -> SIGKILL self

## Caveats / Not Found

- No explicit session "heartbeat" or "keep-alive" between OMP TUI and agent process
- The RPC protocol has no ping/pong or connection health check
- The `GrpcBridge` in `uc-orchestrator` has NO reconnect mechanism -- it creates a single transport at construction time. If the gRPC server restarts (via `run-omp.sh` health_monitor), the bridge's transport is stale
- No explicit file descriptor leak tracking or cleanup
- The `OrchestratorEventEmitter.clear()` is called on `session_shutdown` but there's no guarantee the extension's event handlers are fully cleaned up
