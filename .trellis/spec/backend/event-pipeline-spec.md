# Event Pipeline Spec

## Scenario: Unified NATS Event Pipeline

### 1. Scope / Trigger

- Trigger: Any component that needs to emit task/subtask lifecycle events must use NATS as the single event source. LocalWorker (JSON-RPC) is the sole exception.
- Applies to: Python Orchestrator, Python Worker, Dashboard SSE, Rust gRPC server, TUI

### 2. Signatures

**Python — NatsPublisher.publish_event()**
```python
async def publish_event(
    self,
    event_type: str,
    task_id: str = "",
    subtask_id: str = "",
    data: dict | None = None,
) -> None:
    # Publishes to NATS subject: uc.task.event
    # Payload includes message_id for dedup
```

**Python — Worker._publish_event()**
```python
async def _publish_event(
    self, event_type: str, task_id: str = "",
    subtask_id: str = "", data: dict | None = None,
) -> None:
    # Prefers nats_publisher; falls back to event_emitter
    # (event_emitter only used in LocalWorker / JSON-RPC path)
```

**Rust — TaskStore::check_and_record_message_id()**
```rust
fn check_and_record_message_id(&mut self, msg_id: &str) -> bool
    // Returns false if message was already seen (duplicate)
    // Auto-evicts entries older than 5 minutes when map > 10000
```

**TUI — processEvent() idempotency**
```typescript
const DEDUP_STATUS_EVENTS = new Set([
    "subtask_assigned", "subtask_started",
    "subtask_completed", "subtask_failed",
]);
// Key: `${event.type}:${event.subtaskId ?? ''}:${event.taskId}`
// Max 500 entries in seenEvents
// NOTE: subtask_progress is intentionally NOT deduped — it is transient,
// high-frequency, and each emission carries a distinct phase/percent. Deduping
// would drop intermediate progress updates.
```

### 3. Contracts

**NATS message payload (uc.task.event)**
```json
{
  "message_id": "uc.task.event:1719000000:abc123",
  "type": "subtask_completed",
  "task_id": "task-uuid",
  "subtask_id": "st-uuid",
  "data": { "summary": "...", "success": true }
}
```

| Field | Type | Required | Constraints |
|-------|------|----------|-------------|
| message_id | string | yes | Format: `{subject}:{timestamp_ms}:{random}` |
| type | string | yes | One of: task_submitted, subtask_assigned, subtask_started, subtask_progress, subtask_completed, subtask_failed |
| task_id | string | yes | UUID |
| subtask_id | string | no | UUID, empty for task-level events |
| data | object | no | Event-specific payload |

### 4. Validation & Error Matrix

| Condition | Error / Behavior |
|-----------|------------------|
| Duplicate message_id (Rust) | Skip processing, log debug |
| Duplicate status event (TUI) | Skip state update |
| NATS unavailable (Python) | Fall back to event_emitter (LocalWorker path only) |
| message_id missing | Process normally (no dedup) |
| seen_events map > 500 (TUI) | Evict oldest entries |
| seen_messages map > 10000 (Rust) | Evict entries older than 5 min |

### 5. Good/Base/Bad Cases

- **Good**: NATS → Rust dedup → WatchTask stream → TUI idempotent render
- **Base**: NATS → Dashboard SSE → Browser (no dedup needed, SSE is ephemeral)
- **Bad**: NATS reconnect → duplicate messages → Rust skips duplicates → TUI skips duplicates → no visual glitch

### 6. Tests Required

- [ ] Rust: `check_and_record_message_id` returns true for new, false for duplicate
- [ ] Rust: Eviction works when map exceeds threshold
- [ ] TUI: Status events with same key are skipped
- [ ] TUI: Non-status events (tool_call, file_modified) are never deduped
- [ ] Python: NatsPublisher includes message_id in payload

### 7. Wrong vs Correct

#### Wrong
```python
# Double-write: event_emitter + nats_publisher
await self.event_emitter.emit("subtask_started", ...)
await self.nats_publisher.publish_event("subtask_started", ...)
```

#### Correct
```python
# Single source: NATS only (with LocalWorker fallback)
if self.nats_publisher is not None:
    await self.nats_publisher.publish_event("subtask_started", ...)
elif self.event_emitter is not None:
    await self.event_emitter.emit("subtask_started", ...)
```

---

## Design Decision: Event-driven Dispatch (2026-06-23)

**Context**: `_auto_execute_loop` in SandboxTUI polled every 2 seconds for ready subtasks, and `_execute_subtasks` in NatsWorker polled every 0.5s. Even after a subtask completed, the next ready subtask had to wait for the next poll cycle — adding ~2s latency in local mode and ~0.5s in NATS mode.

**Options Considered**:
1. Keep polling — simple, but adds fixed latency
2. Event-driven with asyncio.Event — immediate wake on subtask completion/failure, with safety timeout
3. Callback-based — worker completion triggers dispatch directly (tighter coupling)

**Decision**: Option 2 — `asyncio.Event` + 30s safety timeout. `_listen_for_events()` (SandboxTUI) and `_handle_task_event()` (NatsWorker) call `self._dispatch_event.set()` when a subtask_completed/subtask_failed event arrives. The execute loop waits on `self._dispatch_event.wait()` instead of `asyncio.sleep()`. A 30s timeout prevents deadlock if events are lost.

**Example**:
```python
# In __init__:
self._dispatch_event: asyncio.Event | None = None

# In start() (inside a running loop):
if self._dispatch_event is None:
    self._dispatch_event = asyncio.Event()

# In event listener / NATS handler:
if event_type in ("subtask_completed", "subtask_failed"):
    self._dispatch_event.set()

# In execute loop:
self._dispatch_event.clear()
try:
    await asyncio.wait_for(self._dispatch_event.wait(), timeout=30.0)
except asyncio.TimeoutError:
    pass  # Safety re-check
```

**Python 3.9 constraint**: `asyncio.Event()` (and `asyncio.Queue()`, `Lock()`, `Semaphore()`) binds a loop at construction on Python ≤3.9 and raises `RuntimeError: There is no current event loop in thread 'MainThread'` when constructed outside a running loop. Although `type-safety.md` targets 3.10+, **CI runs Python 3.9**, so constructing these primitives in `__init__` (synchronous, no loop) breaks 3.9. Construct them lazily in `start()` / first async access. This matches the lazy-`asyncio.Queue()` pattern in `dashboard/app.py`. The three `_handle_memory_changed` tests construct `NatsWorker()` bare (no loop) and regressed on 3.9 until this was fixed.

**Consequences**:
- Subtask dispatch latency: ~2s → <100ms (local), ~0.5s → <100ms (NATS)
- Safety timeout prevents deadlock in edge cases (NATS disconnect, missed events)
- `_dispatch_event` must be cleared before wait to avoid stale wake

---

## Design Decision: Subtask Failure Context — stderr_tail + recent_tool_calls (2026-06-23)

**Context**: When a subtask fails, the `subtask_failed` event only carried an `error` string. This made diagnosing failures difficult — users couldn't see what the agent was doing before it failed or what stderr output it produced.

**Options Considered**:
1. Keep minimal error only — simple but insufficient for debugging
2. Add stderr_tail + recent_tool_calls — structured failure context, bounded size
3. Full stderr + all tool calls — too much data, unbounded

**Decision**: Option 2 — `stderr_tail` (last 10 lines of stderr, truncated to 2000 chars) and `recent_tool_calls` (last 5 tool call names, JSON-serialized for gRPC data map compatibility).

**Example**:
```python
# In Worker.execute_subtask() — building failure event data:
failure_data = {
    "error": result.summary[:300],
    "worker_id": self.worker_id,
}
if result.stderr_tail:
    failure_data["stderr_tail"] = result.stderr_tail
if result.recent_tool_calls:
    failure_data["recent_tools"] = json.dumps(result.recent_tool_calls)  # JSON string for HashMap<String, String>
```

```typescript
// In useTaskEvents processEvent — parsing recent_tools:
const recentTools = Array.isArray(event.data?.recent_tools)
    ? event.data.recent_tools as string[]
    : typeof event.data?.recent_tools === 'string'
        ? JSON.parse(event.data.recent_tools)  // JSON string from gRPC data map
        : undefined;
```

**Gotcha**: `recent_tools` must be JSON-serialized because gRPC `data` is `map<string, string>` (HashMap<String, String> on Rust side). A Python list can't fit into a string-typed map value. The TUI must parse it back from JSON string.

**Consequences**:
- SubtaskResult has new `stderr_tail: str` and `recent_tool_calls: list[str]` fields
- AgentOutput has new `stderr: str` and `tool_calls: list[str]` fields
- Rust `AgentEventType::SubtaskFailed` has new `stderr_tail: String` and `recent_tools: String` fields
- TUI `SubtaskItem` has new `stderrTail?: string` and `recentTools?: string[]` fields
- SubtaskDetail renders stderr (red) and tool call chain (dim)

**Context**: Worker previously supported two execution modes: LLM tool-calling loop (Python-side) and sandbox (Claude Code / Codex CLI). The LLM mode reimplemented a coding agent in Python — redundant with native agents that have superior tool chains.

**Options Considered**:
1. Keep both modes — flexibility for environments without sandbox
2. Delete LLM mode, sandbox-only — simpler, less code, native agents are better
3. Replace LLM mode with a thin wrapper around API — still redundant

**Decision**: Option 2 — sandbox-only. Coding agents (Claude Code, Codex) have complete tool chains (file read/write, search, grep, memory). A Python-side tool-calling loop is strictly inferior.

**Consequences**:
- Worker.py: 1979 → 220 lines
- Orchestrator: no llm_client, rate_limiter, circuit_breaker
- All execution goes through SandboxManager
- Decomposition also sandbox-only (Claude Code reads files itself)
- LLMClient class preserved for potential future lightweight calls

---

## Design Decision: Orchestrator Decomposition — No Pre-Injected Context

**Context**: Orchestrator.decompose_task() previously ran _gather_memory_context() and _gather_code_context() to search for code snippets and inject them into the decomposition prompt. This was wasteful because:
1. The sandbox agent (Claude Code) reads files itself during decomposition
2. Pre-injected context is imprecise — you don't know which subtask needs which code
3. Context window waste — code snippets bloat the prompt without clear benefit

**Options Considered**:
1. Keep full context injection — "more context is better"
2. Inject only project structure summary — lightweight, sufficient for planning
3. No injection at all — agent reads what it needs

**Decision**: Option 2 — inject only project structure (file tree, module names). The sandbox agent handles its own code reading. Planning (decomposition) needs structure, not source.

**Consequences**:
- `_gather_memory_context()` → returns project_id only
- `_gather_code_context()` → returns project_id only
- Decomposition prompt simplified: description + project_id (no code snippets)
- Agent reads files on demand during decomposition and execution

---

## Convention: Event Publishing Priority

**What**: When emitting events, always prefer NATS over local event_emitter.

**Why**: NATS is the unified event bus. Dashboard SSE and Rust gRPC server both consume from NATS. Local event_emitter exists only as a fallback for LocalWorker (no NATS, JSON-RPC path).

**Example**:
```python
# In Worker / Orchestrator
async def _publish_event(self, event_type, **kwargs):
    if self.nats_publisher is not None:
        await self.nats_publisher.publish_event(event_type, **kwargs)
    elif self.event_emitter is not None:
        await self.event_emitter.emit(event_type, **kwargs)
```

**Related**: nats-bridge-spec.md

---

## Contract: subtask_progress event (transient telemetry)

**What**: `subtask_progress` carries real-time execution telemetry — phase, percent, and (for multi-agent workflows) which coding agent is running which step. Unlike lifecycle events (started/completed/failed), it is transient and high-frequency.

**Payload (Python `_progress` helper, worker.py)**:
```json
{
  "type": "subtask_progress",
  "task_id": "task-uuid",
  "subtask_id": "st-uuid",
  "data": {
    "phase": "executing",            // or "step 2/3: codex" for workflow steps
    "percent": 50,
    "worker_id": "worker-1",
    "step_index": 1,                 // optional, workflow only
    "step_total": 3,                 // optional, workflow only
    "step_agent": "codex",           // optional, workflow only (claude-code | codex)
    "step_status": "started",        // optional (started | completed | failed)
    "step_summary": "..."            // optional, truncated
  }
}
```

**Phases** (single-agent path): `preparing(10)` → `executing(50)` → `validating(80)` → `finalizing(95)`.
**Phases** (workflow path): per-step `step N/total: <agent>`, percent = `100 * idx/total`.

**Rust routing**: `nats_event_to_agent_event` (uc-grpc server.rs) must have a `"subtask_progress" =>` match arm returning `Some(AgentEventType::SubtaskProgress{...})`. The `From<AgentEventType> for TaskEventProto` impl (conversions.rs) must serialize it to the proto `data` map with snake_case keys matching the Python payload. `apply_event_to_snapshot` (checkpoint.rs) treats it as a no-op (transient — does not mutate subtask lifecycle state).

> **Warning (Gotcha): Rust match-arm silent drop.** `nats_event_to_agent_event` has a catch-all `_ => None` arm that **silently drops** any event type without an explicit match arm — no log, no error. A new event type published by the Python worker but unhandled in Rust will vanish at the Rust boundary: never enters the broadcast channel, never reaches WatchTask/dashboard/TUI. This is how `subtask_progress` was originally lost (dashboard couldn't show progress despite the worker publishing it). **When adding any new event type: add the match arm in `nats_event_to_agent_event` AND the `From<AgentEventType> for TaskEventProto` arm in the same change**, or the event is silently black-holed. Grep `_ =>` in server.rs to find the catch-all.

---

## Contract: gateway connection_state + reconnect backoff

**What**: GrpcBridge (OMP TUI) and the dashboard gRPC-Web client both maintain a connection to the Rust gRPC server. Connection lifecycle is surfaced via the `connection_state` orchestrator event (`{ connected: boolean; error?: string }`) and the dashboard's `connectionState` state (`connected | connecting | disconnected | error | reconnecting`).

**Reconnect strategy (exponential backoff)**: connection errors (network refused, stream reset) trigger exponential backoff via the shared `backoff.ts` helper (`backoffDelay(attempt, {initialMs, maxMs, maxAttempts})`):
- GrpcBridge `tryReconnect`: 5 attempts, 500ms → 1s → 2s → 4s → 8s, `reconnecting` mutex (only one caller drives the sequence; concurrent callers spin-wait 50ms + re-check). `onConnectionChange(false)` fires at sequence start, `(true)` on success.
- WatchTask stream reconnect (`startWatchTaskStream`): 8 attempts, 500ms → … → 30s cap. Resets attempt counter on first delivered event (stable stream doesn't carry forward accumulated backoff). `bridge.isConnected()` guard prevents tight-loop when server is down.
- **Business errors fail fast** — `isConnectionError` guard (string-match on "refused stream"/"stream error"/"unavailable") scopes backoff to connection errors only; NotFound/InvalidArgument/engine rejections never enter backoff.

**Compose auto-recovery**: gateway container has `restart: unless-stopped` + a TCP healthcheck (`/dev/tcp/localhost:50051`, 10s interval / 5s timeout / 5 retries / 15s start_period). `depends_on: condition: service_healthy` gates dependents. The healthcheck uses bash `/dev/tcp` (bookworm-slim ships bash) — `grpc_health_probe` isn't in the runtime image; TCP liveness is sufficient.

> **Gotcha: WatchTask stream exhaustion**. After `maxAttempts` (8) failed reconnects (~91s cumulative), `sleepBackoff` returns false and the stream stays down until the next RPC-level reconnect (any RPC call triggers `tryReconnect`, which recovers the connection, but the WatchTask stream only resumes on orchestrator restart or a later `onError`). Edge case (gateway down >91s). If permanent stream recovery is needed, add a periodic health-check cycle that calls `startWatchTaskStream` — not implemented in PR5.

> **Gotcha: `maxAttempts` semantics**. `maxAttempts` counts *sleeps between tries*, not total attempts. `maxAttempts: 5` → 6 reconnect attempts with 5 sleeps. Tests use `maxAttempts: 1` (2 attempts, 1 short sleep) to avoid waiting the production curve.

---

## Pattern: NATS Message Dedup

**Problem**: NATS at-least-once delivery can produce duplicate messages, causing duplicate state transitions in Rust TaskStore and duplicate renders in TUI.

**Solution**: Include `message_id` in every NATS message. Rust maintains a `seen_messages: HashMap<String, Instant>` with TTL-based eviction. TUI maintains a `seenEvents: Set<string>` with LRU-style eviction (max 500).

**Example**:
```json
{"message_id": "uc.task.event:1719000000:a1b2", "type": "subtask_completed", ...}
```

```rust
// Rust: skip if already seen
if !self.check_and_record_message_id(&msg_id) {
    return; // duplicate, skip
}
```

```typescript
// TUI: skip duplicate status events
const key = `${event.type}:${event.subtaskId ?? ''}:${event.taskId}`;
if (DEDUP_STATUS_EVENTS.has(event.type) && seenEvents.current.has(key)) return;
```

**Why**: Prevents visual glitches (duplicate log entries, state flicker) without requiring exactly-once semantics from NATS.
