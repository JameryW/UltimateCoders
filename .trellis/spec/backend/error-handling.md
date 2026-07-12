# Error Handling

> How errors are defined, propagated, mapped across boundaries, and recovered.

---

## Overview

The project uses a single `EngineError` enum defined in `uc-types` as the shared error contract. This enum is mapped to three different error representations depending on the consumer:

1. **Rust internal** -- `EngineError` propagated via `Result<_, EngineError>`
2. **Python boundary** -- Mapped to native Python exception types via PyO3
3. **gRPC boundary** -- Mapped to tonic `Status` codes

---

## EngineError Enum

Defined in `crates/uc-types/src/error.rs:9-52`. 14 variants, all using `thiserror::Error` derive:

```rust
#[derive(Debug, Error)]
pub enum EngineError {
    #[error("Not found: {0}")]
    NotFound(String),

    #[error("Search failed: {0}")]
    SearchError(String),

    #[error("Index not found: {0}")]
    IndexError(String),

    #[error("Memory read error: {0}")]
    MemoryReadError(String),

    #[error("Memory write error: {0}")]
    MemoryWriteError(String),

    #[error("Indexing failed: {0}")]
    IndexingError(String),

    #[error("Connection error: {0}")]
    ConnectionError(String),

    #[error("Timeout: {0}")]
    TimeoutError(String),

    #[error("Rate limited: retry after {0}s")]
    RateLimited(u64),                          // Carries retry-after seconds, not a String

    #[error("Conflict detected in {path}: {details}")]
    ConflictError { path: String, details: String },  // Named fields, not a String

    #[error("Task failed: {0}")]
    TaskError(String),

    #[error("Worker unavailable: {0}")]
    WorkerUnavailable(String),

    #[error("Sandbox error: {0}")]
    SandboxError(String),

    #[error("Configuration error: {0}")]
    ConfigError(String),

    #[error("Internal error: {0}")]
    InternalError(String),
}
```

**Design notes**:
- Most variants carry a `String` message
- `NotFound(String)` is the unified "resource does not exist" variant — used instead of `IndexError` or `SearchError` for not-found semantics
- `RateLimited(u64)` carries the retry-after duration in seconds (not a string)
- `ConflictError { path, details }` uses named struct-like fields for clarity
- `MemoryReadError` and `MemoryWriteError` are separate variants (not combined)

---

## Helper Methods

`EngineError` provides three classification methods (`crates/uc-types/src/error.rs:54-80`):

```rust
impl EngineError {
    /// Whether this error is retryable (rate limit, timeout, connection).
    pub fn is_retryable(&self) -> bool {
        matches!(self, EngineError::RateLimited(_) | EngineError::TimeoutError(_) | EngineError::ConnectionError(_))
    }

    /// Whether this error suggests model fallback.
    pub fn should_fallback(&self) -> bool {
        matches!(self, EngineError::RateLimited(_) | EngineError::TimeoutError(_))
    }

    /// Whether this error represents a resource-not-found condition.
    pub fn is_not_found(&self) -> bool {
        matches!(self, EngineError::NotFound(_))
    }
}
```

These are used by the Python-side rate limiter and gRPC fallback logic to decide retry/fallback/degradation behavior.

---

## Rust-to-Python Mapping

Defined in `crates/uc-python/src/engine.rs:26-49`. Each `EngineError` variant maps to a specific Python exception:

| EngineError Variant | Python Exception | Rationale |
|---|---|---|
| `NotFound` | `PyKeyError` | Resource not found semantics |
| `SearchError` | `PyRuntimeError` | General runtime failure |
| `IndexError` | `PyKeyError` | "Not found" semantics (legacy — prefer `NotFound` for new code) |
| `MemoryReadError` / `MemoryWriteError` | `PyRuntimeError` | General runtime failure |
| `IndexingError` | `PyRuntimeError` | General runtime failure |
| `ConnectionError` | `PyConnectionError` | Network/connection semantics |
| `TimeoutError` | `PyTimeoutError` | Timeout semantics |
| `RateLimited(secs)` | `PyRuntimeError` ("Rate limited, retry after Ns") | Message includes retry-after |
| `ConflictError` | `PyRuntimeError` ("Conflict in path: details") | Message includes path + details |
| `TaskError` | `PyRuntimeError` | General runtime failure |
| `WorkerUnavailable` | `PyConnectionError` | "Not available" semantics |
| `SandboxError` | `PyPermissionError` | Permission/security semantics |
| `ConfigError` | `PyValueError` | "Invalid input" semantics |
| `InternalError` | `PyRuntimeError` | Catch-all |

---

## Rust-to-gRPC Mapping

Defined in `crates/uc-grpc/src/server.rs:30-48`. Each variant maps to a tonic `Code`:

| EngineError Variant | tonic Code | Rationale |
|---|---|---|
| `NotFound` | `NotFound` | Standard gRPC NOT_FOUND |
| `SearchError` | `Internal` | Server-side search failure |
| `IndexError` | `NotFound` | Index/resource not found (legacy — prefer `NotFound` for new code) |
| `MemoryReadError` / `MemoryWriteError` | `Internal` | Server-side failure |
| `IndexingError` | `Internal` | Server-side failure |
| `ConnectionError` | `Unavailable` | Backend unreachable |
| `TimeoutError` | `DeadlineExceeded` | Deadline semantics |
| `RateLimited(secs)` | `ResourceExhausted` | Resource limit semantics |
| `ConflictError` | `Aborted` | Conflict semantics |
| `TaskError` | `FailedPrecondition` | State precondition violation |
| `WorkerUnavailable` | `Unavailable` | Backend unreachable |
| `SandboxError` | `PermissionDenied` | Permission semantics |
| `ConfigError` | `InvalidArgument` | Bad input semantics |
| `InternalError` | `Internal` | Catch-all |

The function returns `Status::new(code, msg)` where `msg` is the original error message (or a formatted version for `RateLimited` and `ConflictError`).

---

## Error Wrapping Pattern

Storage clients wrap third-party errors into `EngineError` variants using `.map_err()`:

**TiKV read** (`crates/uc-engine/src/memory/short_term.rs:100`):
```rust
.map_err(|e| EngineError::MemoryReadError(format!("TiKV read error: {}", e)))?
```

**PostgreSQL migration** (`crates/uc-engine/src/metadata/postgres.rs:156`):
```rust
.map_err(|e| EngineError::ConnectionError(format!("Migration error (repos): {}", e)))?
```

**PostgreSQL repo insert** (`crates/uc-engine/src/metadata/postgres.rs:275`):
```rust
.map_err(|e| EngineError::ConnectionError(format!("Repo insert error: {}", e)))?
```

**Pattern**: Always include the original error in the formatted message for debugging. Use the most specific `EngineError` variant:
- Database/connection issues -> `ConnectionError`
- Read/write failures -> `MemoryReadError` / `MemoryWriteError`
- Search failures -> `SearchError`
- Invalid config -> `ConfigError`

---

## Error Recovery Patterns

### Best-effort secondary writes

When writing to long-term memory, failures are logged but do not propagate since the short-term write succeeded (`crates/uc-engine/src/memory/mod.rs:100-115`):

```rust
if entry.metadata.importance >= self.config.long_term_importance_threshold {
    match self.long_term.write(&entry).await {
        Ok(()) => { tracing::debug!("Wrote entry to long-term memory"); }
        Err(e) => {
            tracing::warn!("Failed to write to long-term memory: {}", e);
            // Do NOT fail -- short-term write succeeded
        }
    }
}
```

### Graceful degradation on embedding failure

When `EmbeddingService` fails to generate an embedding (API timeout, BLAKE3 hash error, etc.), the operation returns empty/default results with a warning log rather than propagating an `EngineError`. This pattern applies to `MemoryStore::search()`, `MemoryStore::read()`, and `MemoryStore::write()`:

```rust
// search(): return empty results
let query_embedding = match self.embedding_service.embed_single(&request.query).await {
    Ok(vec) => vec,
    Err(e) => {
        tracing::warn!("Embedding generation failed, returning empty results: {}", e);
        return Ok(MemorySearchResponse { results: vec![] });
    }
};

// write(): fall back to zero vector (entry still stored, just not findable by semantic search)
let embedding = match self.embedding_service.embed_single(&content_text).await {
    Ok(vec) => Some(vec),
    Err(e) => {
        tracing::warn!("Embedding generation failed for write, using zero vector: {}", e);
        None
    }
};
```

**Why this pattern**: Embedding generation is an enhancement, not a hard requirement. The system must still function (with degraded search quality) when the embedding service is unavailable. Returning `EngineError` would break callers that don't handle search failures gracefully.

---

## Forbidden Patterns

1. **Never use `unwrap()` on Result in production code** -- Use `.map_err()` to convert to `EngineError`, or `.ok()` / `.map()` to handle None cases.

2. **Never let raw third-party errors escape the API boundary** -- Always wrap via `.map_err(|e| EngineError::Variant(format!("...: {}", e)))`.

3. **Never catch and suppress errors silently** -- If an error is non-critical, at minimum use `tracing::warn!()` to log it (see the best-effort pattern above).

4. **Never create new error types outside `uc-types/src/error.rs`** -- All engine errors must be `EngineError` variants. Domain-specific crates (config, sandbox) may have their own private error enums for internal use, but these must be mapped to `EngineError` at the API boundary.

---

## gRPC Fallback Mode

The Python `Engine` class supports automatic gRPC-to-local fallback (`python/ultimate_coders/engine.py`):

### Configuration

```python
Engine(mode="grpc", grpc_endpoint="localhost:50051", fallback_mode="auto")
```

- `fallback_mode="none"` (default): no fallback, gRPC errors propagate
- `fallback_mode="auto"`: on `ConnectionError`/`TimeoutError`/`OSError`, automatically switch to local engine

### State Machine

```
grpc_ok → grpc_failed → local_active → grpc_recovered → grpc_ok
```

- **grpc_failed**: detected on any gRPC call failure → activates local engine
- **local_active**: all Engine API calls go through local engine
- **grpc_recovered**: health check succeeds (checked every 30s) → switches back to gRPC

### Callbacks

```python
engine = Engine(
    mode="grpc",
    fallback_mode="auto",
    on_fallback=lambda: print("Fell back to local mode"),
    on_recovery=lambda: print("Recovered gRPC connection"),
)
```

### Contract

- Fallback is transparent: callers see the same `Engine` API
- Local engine may have reduced functionality (no shared state across processes)
- `engine.fallback_active` property returns `True` when in local fallback mode
- Backward compatible: `fallback_mode="none"` preserves original behavior

---

## Health Check Components

`LocalEngine.health()` returns component-level status in `HealthStatus.components`:

| Component | Status Values | Meaning |
|-----------|--------------|---------|
| `memory_short_term` | healthy / degraded / unavailable | TiKV or in-memory fallback |
| `memory_long_term` | healthy / degraded / unavailable | Qdrant or in-memory fallback |
| `metadata` | healthy / degraded / unavailable | PostgreSQL or in-memory fallback |
| `search` | healthy / degraded | Text + semantic search engine |
| `index_pipeline` | healthy / unavailable | AST + text indexing |

- **degraded**: using in-memory fallback (data not persisted across restarts)
- **unavailable**: component is non-functional
- **healthy**: using real storage backend

Overall `HealthStatus.status` is derived from the worst component status.

gRPC health reflection (`tonic-health`) is also registered, allowing standard `grpc.health.v1.Health` checks.

---

## Common Mistakes

1. **Mapping `MemoryWriteError` for delete operations** -- Deletes that fail should also use `MemoryWriteError` (this is the current convention in `short_term.rs:173`), even though "delete" is not "write". The naming is about KV mutation, not CRUD semantics.

2. **Forgetting to format the original error** -- Just `EngineError::ConnectionError("error")` loses the actual cause. Always include the original error: `format!("TiKV read error: {}", e)`.

3. **Using `IndexError` or `SearchError` for "not found" semantics** -- New code should use `EngineError::NotFound(msg)` instead. `IndexError` maps to `PyKeyError` and gRPC `NOT_FOUND` for backward compatibility, but `NotFound` is the canonical variant. Empty search results are NOT errors — only single-key lookups that fail should return `NotFound`.