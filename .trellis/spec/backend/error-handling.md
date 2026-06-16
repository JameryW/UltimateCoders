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

Defined in `crates/uc-types/src/error.rs:9-52`. 13 variants, all using `thiserror::Error` derive:

```rust
#[derive(Debug, Error)]
pub enum EngineError {
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
- `RateLimited(u64)` carries the retry-after duration in seconds (not a string)
- `ConflictError { path, details }` uses named struct-like fields for clarity
- `MemoryReadError` and `MemoryWriteError` are separate variants (not combined)

---

## Helper Methods

`EngineError` provides two classification methods (`crates/uc-types/src/error.rs:54-69`):

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
}
```

These are used by the Python-side rate limiter and circuit breaker to decide retry/fallback behavior.

---

## Rust-to-Python Mapping

Defined in `crates/uc-python/src/engine.rs:26-49`. Each `EngineError` variant maps to a specific Python exception:

| EngineError Variant | Python Exception | Rationale |
|---|---|---|
| `SearchError` | `PyRuntimeError` | General runtime failure |
| `IndexError` | `PyKeyError` | "Not found" semantics |
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
| `SearchError` | `Internal` | Server-side search failure |
| `IndexError` | `NotFound` | Index/resource not found |
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

### Circuit breaker integration

The Python-side `CircuitBreaker` class uses `EngineError.is_retryable()` and `should_fallback()` to determine whether to retry, fall back to a different model, or open the circuit.

---

## Forbidden Patterns

1. **Never use `unwrap()` on Result in production code** -- Use `.map_err()` to convert to `EngineError`, or `.ok()` / `.map()` to handle None cases.

2. **Never let raw third-party errors escape the API boundary** -- Always wrap via `.map_err(|e| EngineError::Variant(format!("...: {}", e)))`.

3. **Never catch and suppress errors silently** -- If an error is non-critical, at minimum use `tracing::warn!()` to log it (see the best-effort pattern above).

4. **Never create new error types outside `uc-types/src/error.rs`** -- All engine errors must be `EngineError` variants. Domain-specific crates (config, sandbox) may have their own private error enums for internal use, but these must be mapped to `EngineError` at the API boundary.

---

## Common Mistakes

1. **Mapping `MemoryWriteError` for delete operations** -- Deletes that fail should also use `MemoryWriteError` (this is the current convention in `short_term.rs:173`), even though "delete" is not "write". The naming is about KV mutation, not CRUD semantics.

2. **Forgetting to format the original error** -- Just `EngineError::ConnectionError("error")` loses the actual cause. Always include the original error: `format!("TiKV read error: {}", e)`.