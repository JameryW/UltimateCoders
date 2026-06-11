# Research: Codebase Patterns

- **Query**: Analyze UltimateCoders codebase to discover real patterns for filling .trellis/spec/ guidelines
- **Scope**: Internal
- **Date**: 2026-06-11

## Findings

### Backend: Directory Structure

The 5 Rust crates follow a clear separation of concerns:

| Directory | Purpose |
|---|---|
| `crates/uc-types/src/` | Shared types + `EngineApi` trait (error.rs, engine.rs, memory.rs, search.rs, agent.rs, index.rs) |
| `crates/uc-engine/src/` | Core engine implementation — `LocalEngine` implements `EngineApi` |
| `crates/uc-grpc/src/` | gRPC server/client + proto-to-types conversions |
| `crates/uc-grpc-server/src/` | Standalone gRPC server binary (main.rs) |
| `crates/uc-python/src/` | PyO3 Python binding (engine.rs, types.rs, async_support.rs) |

**uc-engine sub-module layout** (`crates/uc-engine/src/lib.rs:6-19`):

| Sub-module | Files | Purpose |
|---|---|---|
| `memory/` | `mod.rs`, `short_term.rs`, `long_term.rs` | Layered memory (TiKV short-term, Qdrant long-term) |
| `indexer/` | `mod.rs`, `text.rs`, `ast.rs`, `semantic.rs` | Index pipeline (text tokenizer, tree-sitter AST, BLAKE3/Voyage embeddings) |
| `search/` | `mod.rs`, `hybrid.rs`, `semantic.rs` | Hybrid search engine (text + AST + semantic) |
| `sandbox/` | `mod.rs`, `subprocess.rs`, `docker.rs`, `pool.rs`, `file_tracker.rs`, `agents/` | Sandbox execution (subprocess, Docker, agent adapters) |
| `sandbox/agents/` | `mod.rs`, `claude_code.rs`, `codex.rs` | Coding agent adapters |
| `metadata/` | `mod.rs`, `postgres.rs` | PostgreSQL structured metadata |
| `git/` | `mod.rs`, `repo_manager.rs` | Git repository management |
| `scheduler/` | `mod.rs`, `orchestrator.rs` | Task scheduling |
| `conflict/` | `mod.rs`, `merger.rs` | Conflict detection + three-way merge |
| Top-level | `local.rs`, `config.rs`, `events.rs`, `checkpoint.rs`, `rate_limiter.rs`, `circuit_breaker.rs` | LocalEngine, config, fault tolerance |

**Pattern**: Each sub-module has a `mod.rs` that declares public API + sub-files for implementation details. The `lib.rs` re-exports key types via `pub use`.

---

### Backend: Error Handling

**`EngineError` enum** (`crates/uc-types/src/error.rs:9-52`):
- 13 variants, all use `thiserror::Error` derive
- Most variants carry a `String` message; `RateLimited` carries `u64` (retry-after seconds); `ConflictError` carries named fields `{ path, details }`
- Two helper methods: `is_retryable()` (line 56) and `should_fallback()` (line 66)

**Rust-to-Python mapping** (`crates/uc-python/src/engine.rs:26-49`):
```rust
fn engine_error_to_pyerr(err: EngineError) -> PyErr {
    match err {
        SearchError(msg) => PyRuntimeError,
        IndexError(msg) => PyKeyError,
        ConnectionError(msg) => PyConnectionError,
        TimeoutError(msg) => PyTimeoutError,
        RateLimited(secs) => PyRuntimeError("Rate limited, retry after Ns"),
        ConflictError { path, details } => PyRuntimeError("Conflict in path: details"),
        SandboxError(msg) => PyPermissionError,
        ConfigError(msg) => PyValueError,
        // others -> PyRuntimeError
    }
}
```

**Rust-to-gRPC mapping** (`crates/uc-grpc/src/server.rs:30-48`):
```rust
fn to_status(err: EngineError) -> Status {
    match &err {
        SearchError -> tonic::Code::Internal,
        IndexError -> tonic::Code::NotFound,
        ConnectionError -> tonic::Code::Unavailable,
        TimeoutError -> tonic::Code::DeadlineExceeded,
        RateLimited -> tonic::Code::ResourceExhausted,
        ConflictError -> tonic::Code::Aborted,
        TaskError -> tonic::Code::FailedPrecondition,
        WorkerUnavailable -> tonic::Code::Unavailable,
        SandboxError -> tonic::Code::PermissionDenied,
        ConfigError -> tonic::Code::InvalidArgument,
        // others -> tonic::Code::Internal
    }
}
```

**Error wrapping pattern**: Storage clients wrap third-party errors into `EngineError` variants using `.map_err()`:
- `crates/uc-engine/src/memory/short_term.rs:100`: `map_err(|e| EngineError::MemoryReadError(format!("TiKV read error: {}", e)))`
- `crates/uc-engine/src/metadata/postgres.rs:156`: `map_err(|e| EngineError::ConnectionError(format!("Migration error (repos): {}", e)))`

---

### Backend: Database/Storage Patterns

**Three storage backends** with identical structural pattern:

1. **TiKV** (short-term memory) — `crates/uc-engine/src/memory/short_term.rs`
2. **Qdrant** (long-term memory) — `crates/uc-engine/src/memory/long_term.rs`
3. **PostgreSQL** (metadata) — `crates/uc-engine/src/metadata/postgres.rs`

**Universal fallback pattern** (all three follow this):
```rust
pub struct Store {
    #[cfg(feature = "storage")]
    client: Option<Arc<RealClient>>,       // Real client when available
    fallback: Arc<RwLock<FallbackData>>,    // In-memory fallback always present
}
```

**Construction pattern** (3 variants each):
1. `new(endpoint)` — tries to connect, falls back to in-memory if unavailable (e.g., `short_term.rs:40-62`)
2. `new_fallback()` — always creates in-memory only (e.g., `short_term.rs:82-89`)
3. `with_client(client)` — dependency injection with existing client (e.g., `short_term.rs:72-79`)

**Dual-path read/write** (every method uses `#[cfg]` + `if let Some(client))`:
```rust
pub async fn read(&self, key: &MemoryKey) -> Result<...> {
    #[cfg(feature = "storage")]
    if let Some(client) = &self.client {
        // Real storage path
    } else {
        // Fallback path
    }
    #[cfg(not(feature = "storage"))]
    {
        // Fallback path (duplicated)
    }
}
```

**Connection logging pattern** (`short_term.rs:43-51`):
- Success: `tracing::info!("Connected to TiKV for short-term memory")`
- Fallback: `tracing::warn!("TiKV unavailable, using in-memory fallback: {}", e)`

**Health check pattern** (`memory/mod.rs:199-228`):
- `is_connected()` returns `bool`
- `health()` returns `Vec<ComponentHealth>` with status "ok" or "fallback"

**Key encoding pattern** (`short_term.rs:262-274`):
- TiKV keys: `memory:{scope}:{scope_id}:{key}` (e.g., `memory:task:abc123:decisions`)
- Qdrant payloads: `task:{task_id}:{key}`, `project:{project_id}:{key}`, `global:{key}`

**PostgreSQL schema** (`metadata/postgres.rs:141-233`):
- Tables: `repos`, `index_state`, `symbols`, `references`
- Migrations run at startup via `run_migrations()`
- Indexes on `repo_id`, `name`, `kind`, `file_path`, `target_name`

---

### Backend: Logging

**Rust — `tracing` crate** (not `log`):
- `tracing::info!()` — successful connections, significant state changes
- `tracing::warn!()` — fallback activations, non-critical failures
- `tracing::debug!()` — detailed operation info (e.g., memory write importance scores)
- `tracing::error!()` — not observed in current codebase (errors propagated via Result instead)

**Examples**:
- `memory/mod.rs:103`: `tracing::debug!("Wrote entry to long-term memory (importance={:.2})", entry.metadata.importance)`
- `memory/mod.rs:110`: `tracing::warn!("Failed to write to long-term memory: {}", e)`
- `circuit_breaker.rs:170`: `tracing::warn!("Circuit breaker half-open: test request failed, re-opening")`

**Python — stdlib `logging`**:
- Pattern: `logger = logging.getLogger(__name__)` at module level
- Used in: `orchestrator.py:42`, `worker.py:51`, `llm.py:16`, `conflict.py:20`, `sandbox.py:15`, `rate_limiter.py:16`
- Levels used: `logger.info()`, `logger.warning()`, `logger.error()`, `logger.debug()`
- Pattern for error handling with logging: `logger.warning("...", exc_info=True)` (orchestrator.py:161)
- Pattern for debug-only: `logger.debug("...", exc_info=True)` (orchestrator.py:517)

---

### Backend: Quality/Testing

**Test count**: ~240 Rust test functions across all crates.

**Rust test patterns**:
1. **Inline module tests** — `#[cfg(test)] mod tests { ... }` at bottom of each file
2. **Helper factories** — `make_entry()`, `make_store()`, `make_write_request()` reduce boilerplate
3. **Fallback-based testing** — `LocalEngine::new_fallback()` and `ShortTermMemory::new_fallback(3600)` for tests without infra
4. **Async tests** — `#[tokio::test]` for all async operations
5. **Naming convention** — `test_{unit}_{scenario}` (e.g., `test_fallback_read_write_delete`, `test_encode_decode_task_key`)
6. **Assertions** — `assert_eq!`, `assert!`, `assert!(result.is_some())`, `assert!(matches!(result, ConflictResult::Conflicting { .. }))`
7. **Feature-gated tests** — `#[cfg(feature = "indexing")]` for tests requiring specific features (local.rs:695, 768)
8. **gRPC error mapping tests** — `crates/uc-grpc/src/server.rs:178-221`: verify each `EngineError` variant maps to the correct tonic `Code`

**Python test patterns** (`tests/python/test_agent.py`):
1. **pytest** with `class Test*` grouping
2. **unittest.mock** — `MagicMock`, `AsyncMock` for engine/LLM mocking
3. **`@pytest.mark.asyncio`** for async tests
4. **Helper factories** — `_make_engine()` returns a mocked engine
5. **Assertions** — `assert result is None`, `assert "text" in result`, `pytest.raises(ValueError, match="...")`
6. **Integration-style tests** — `TestOrchestratorWorkerIntegration` class tests cross-component flows

---

### Frontend (Python Agent Layer): Directory Structure

```
python/ultimate_coders/
    __init__.py           # Top-level exports (Engine, Orchestrator, Worker, Memory types)
    config.py             # Configuration dataclasses (EngineConfig, StorageConfig, etc.)
    engine.py             # Engine factory — switches between local/gRPC mode
    agent/
        __init__.py       # Re-exports all agent types
        types.py          # Data classes (Task, Subtask, WorkerInfo, enums)
        orchestrator.py   # Orchestrator — task decomposition + worker coordination
        worker.py         # Worker — subtask execution with LLM + tools
        llm.py            # LLM client abstraction (Anthropic API)
        conflict.py       # Conflict detection/resolution (Python-side)
        rate_limiter.py   # Rate limiting + circuit breaker (Python-side)
        sandbox.py        # Sandbox agent execution (Claude Code / Codex adapters)
    memory/
        __init__.py
        memory.py         # MemoryKey, MemoryEntry, ShortTermMemory, LongTermMemory wrappers
    search/
        __init__.py
        query.py          # SearchQuery builder pattern
        result.py         # SearchResultItem, SearchResult dataclasses
```

---

### Frontend: Component Patterns

**Dataclass pattern** — all data types use `@dataclass` (not TypedDict):
- `types.py:39-147`: `FileChange`, `SubtaskResult`, `Subtask`, `Task`, `WorkerInfo`, `OrchestratorConfig`
- `memory.py:19-60`: `MemoryKey`, `MemoryEntry`
- `result.py:10-40`: `SearchResultItem`, `SearchResult`
- `config.py:13-54`: `EngineConfig`, `StorageConfig`, `NatsConfig`, `LlmConfig`, `Config`
- `llm.py:19-43`: `ToolDefinition`, `ToolCall`, `LLMResponse`

**Enum pattern** — status/type enums inherit from `Enum`:
- `types.py:12-37`: `TaskStatus`, `SubtaskStatus`, `ChangeType` — string values (e.g., `"in_progress"`)

**Builder pattern** — `SearchQuery` uses fluent builder:
- `query.py:8-51`: `SearchQuery("auth").in_repos([...]).in_languages([...]).with_modes([...]).limit(20).to_dict()`

**Adapter pattern** — Engine wraps PyO3 or gRPC:
- `engine.py:18-285`: `Engine` class with `PyEngine` from Rust extension
- `memory.py:147-415`: `ShortTermMemory`/`LongTermMemory` delegate to `Engine`

**from_rust / from_dict pattern** — `MemoryEntry.from_rust(raw)` and `MemoryEntry.from_dict(data)` for dual-source construction (`memory.py:62-144`)

---

### Frontend: State Management

**Task state machine** (mirrors Rust `TaskStatus`):
- `TaskStatus`: CREATED -> PLANNING -> IN_PROGRESS -> COMPLETED/FAILED/PAUSED
- `SubtaskStatus`: PENDING -> ASSIGNED -> IN_PROGRESS -> COMPLETED/FAILED/CONFLICTED

**State managed in-memory** on the Orchestrator:
- `orchestrator.py:117-118`: `self.workers: Dict[str, WorkerInfo]` and `self.tasks: Dict[str, Task]`
- Task state transitions in `submit_task()` (line 123-174): creates Task, sets PLANNING, decomposes, sets IN_PROGRESS
- Subtask state transitions in `assign_subtask()` (line 217-277): sets ASSIGNED, increments worker load
- Result handling in `handle_subtask_result()` (line 279-357): sets COMPLETED/FAILED, decrements load, checks if overall task done

**Computed properties on dataclasses**:
- `Task.is_complete` (types.py:104): all subtasks completed
- `Task.has_failed` (types.py:113): any subtask failed
- `Task.ready_subtasks` (types.py:117): pending subtasks with all deps completed
- `Subtask.is_ready` (types.py:72): status == PENDING
- `WorkerInfo.is_available` (types.py:139): current_load < max_capacity

**Memory persistence** — state is persisted to engine memory (if available):
- `orchestrator.py:149-161`: writes task definition to memory on submit
- `orchestrator.py:256-273`: writes assignment to memory on assign
- `orchestrator.py:329-340`: writes result to memory on completion

---

### Frontend: Type Safety

**Type annotation patterns**:
- `from __future__ import annotations` — used in every Python file (enables PEP 604 style)
- `Optional[str]` — for nullable fields (not `str | None`)
- `List[str]` — for collections (not `list[str]`)
- `Dict[str, WorkerInfo]` — for mappings (not `dict[str, WorkerInfo]`)
- `Any` — for engine parameter (e.g., `engine: Any = None` in orchestrator.py:97) since it can be Rust `PyEngine` or Python `Engine`
- `**kwargs: Any` — used in tool methods for extensibility

**Default factory pattern** for mutable defaults:
- `field(default_factory=list)` — for `List` fields
- `field(default_factory=lambda: str(uuid.uuid4()))` — for auto-generated IDs
- `field(default_factory=lambda: datetime.now(timezone.utc))` — for timestamps

**Optional with default None**:
- `assigned_worker: Optional[str] = None` (types.py:65)
- `result: Optional[SubtaskResult] = None` (types.py:69)
- `project_id: Optional[str] = None` (engine.py:169)

**Engine type erasure**: The `engine` parameter is typed as `Any` throughout because it can be either the Python `Engine` wrapper or a raw `PyEngine` from the Rust extension. This is a deliberate design tradeoff.

---

## Caveats / Not Found

- No explicit `tracing::error!()` calls found in Rust codebase — errors are propagated via `Result<_, EngineError>` instead of logged
- Python tests do not test the Rust extension directly (they mock the engine)
- The `storage` feature flag is not tested in CI (requires TiKV/Qdrant/PostgreSQL infrastructure)
- No TypedDict usage found anywhere in the Python layer — dataclasses are used exclusively
- No `py.typed` marker file found for PEP 561 type checking support
