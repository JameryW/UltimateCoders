# Logging Guidelines

> How logging is done in the Rust and Python layers of this project.

---

## Overview

The project uses two logging systems:

- **Rust**: `tracing` crate (not `log`) -- structured, async-aware logging
- **Python**: stdlib `logging` module -- `logging.getLogger(__name__)` at module level

Errors are generally **propagated via Result** rather than logged. The `tracing::error!()` macro is not used in the current codebase -- failed operations return `Err(EngineError::...)` and the caller decides whether to log or propagate.

---

## Rust Logging (tracing crate)

### Log Levels and When to Use Each

| Level | When to Use | Example |
|-------|-------------|---------|
| `tracing::info!()` | Successful connections, significant state changes | Storage backend connected |
| `tracing::warn!()` | Fallback activations, non-critical failures, best-effort operations that failed | TiKV unavailable, long-term memory write failed |
| `tracing::debug!()` | Detailed operation info, diagnostic data | Memory write importance scores, internal state |
| `tracing::error!()` | **Not used** in current codebase. Errors are propagated via `Result` instead. | (none) |

### Real Examples

**info** -- Successful storage connection (`crates/uc-engine/src/memory/short_term.rs:43`):
```rust
tracing::info!("Connected to TiKV for short-term memory");
```

**info** -- Migrations completed (`crates/uc-engine/src/metadata/postgres.rs:242`):
```rust
tracing::info!("PostgreSQL migrations completed");
```

**warn** -- Fallback activation (`crates/uc-engine/src/memory/short_term.rs:51-54`):
```rust
tracing::warn!("TiKV unavailable, using in-memory fallback for short-term memory: {}", e);
```

**warn** -- Best-effort operation failed (`crates/uc-engine/src/memory/mod.rs:110`):
```rust
tracing::warn!("Failed to write to long-term memory: {}", e);
```

**warn** -- Circuit breaker state change (`crates/uc-engine/src/circuit_breaker.rs:170-173`):
```rust
tracing::warn!("Circuit breaker opened after {} failures", failures);
```

**debug** -- Detailed operation info (`crates/uc-engine/src/memory/mod.rs:103-106`):
```rust
tracing::debug!("Wrote entry to long-term memory (importance={:.2})", entry.metadata.importance);
```

---

## Python Logging (stdlib)

### Pattern

Every Python module creates a module-level logger:

```python
import logging
logger = logging.getLogger(__name__)
```

**Real examples**:
- `python/ultimate_coders/agent/orchestrator.py:42`: `logger = logging.getLogger(__name__)`
- `python/ultimate_coders/agent/worker.py:51`: `logger = logging.getLogger(__name__)`
- `python/ultimate_coders/agent/llm.py:16`
- `python/ultimate_coders/agent/conflict.py:20`
- `python/ultimate_coders/agent/sandbox.py:15`
- `python/ultimate_coders/agent/rate_limiter.py:16`

### Log Levels

| Level | When to Use | Example |
|-------|-------------|---------|
| `logger.info()` | Significant state transitions | Task created, worker registered |
| `logger.warning()` | Non-critical failures, degraded operation | Memory write failed, rate limit approaching |
| `logger.error()` | Critical failures that affect task outcome | Task decomposition failed |
| `logger.debug()` | Detailed diagnostic info | Tool execution, internal decisions |

### Error Logging with Traceback

When logging exceptions, always use `exc_info=True`:

```python
# orchestrator.py:161 -- non-critical failure, log warning with traceback
logger.warning("Failed to write task to memory", exc_info=True)

# orchestrator.py:517 -- debug-level diagnostic
logger.debug("...", exc_info=True)

# orchestrator.py:169 -- critical failure
logger.error("Failed to decompose task %s", task.id, exc_info=True)
```

### Format Strings

Python uses `%s` style formatting (not f-strings) in log calls:

```python
# Correct
logger.error("Failed to decompose task %s", task.id, exc_info=True)

# Avoid -- f-strings in log calls compute the string even if the log level is disabled
logger.error(f"Failed to decompose task {task.id}", exc_info=True)
```

---

## What to Log

| Category | What | Level |
|----------|------|-------|
| Storage connections | Connected / fallback | info / warn |
| Migrations | Completed | info |
| Best-effort failures | Secondary write/delete failed | warn |
| Circuit breaker | State transitions (open, half-open, re-opened) | warn |
| Memory importance | Debug info on importance thresholds | debug |
| Task lifecycle | Created, decomposed, completed, failed | info / error |
| Worker lifecycle | Registered, unregistered, heartbeat | info / debug |

---

## What NOT to Log

- **API keys or secrets** -- Never log `api_key`, `voyage_api_key`, or connection strings with credentials
- **Full content of memory entries** -- Log metadata (key, importance) but not the content body
- **Full embedding vectors** -- Log dimensions/count only, not the actual float values
- **Raw protobuf/gRPC message bodies** -- Log request type and key identifiers only

---

## Common Mistakes

1. **Using `log` crate instead of `tracing`** -- The project uses `tracing` throughout. The `log` crate is not imported anywhere in the engine code.

2. **Using `tracing::error!()` for expected failures** -- Expected operational errors (connection refused, timeout) should use `warn!()` with fallback handling. Reserve `error!()` for truly unexpected, unrecoverable situations (if any).

3. **Using f-strings in Python log calls** -- Use `%s` style to avoid computing the string when the log level is disabled.

4. **Logging without `exc_info=True` in exception handlers** -- If you catch an exception and log it, always include `exc_info=True` so the traceback is preserved.
