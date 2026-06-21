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
| type | string | yes | One of: task_submitted, subtask_assigned, subtask_started, subtask_completed, subtask_failed |
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

## Design Decision: Sandbox-Only Worker Execution

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
