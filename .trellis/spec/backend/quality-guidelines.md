# Quality Guidelines

> Code quality standards, testing patterns, naming conventions, and review checklist.

---

## Overview

The project has ~240 Rust test functions across all crates and a Python test suite under `tests/python/`. Rust tests use the `#[cfg(test)] mod tests` inline pattern. Python tests use `pytest` with `unittest.mock`.

---

## Rust Test Patterns

### 1. Inline Module Tests

Every Rust source file places tests in an inline `#[cfg(test)] mod tests` block at the bottom of the file:

```rust
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_encode_decode_task_key() { ... }

    #[tokio::test]
    async fn test_fallback_read_write_delete() { ... }
}
```

### 2. Helper Factories

Tests use helper functions to reduce boilerplate. These are defined inside the `mod tests` block:

**Memory entry factory** (`crates/uc-engine/src/memory/short_term.rs:344-358`):
```rust
fn make_entry(key: MemoryKey, content: &str) -> MemoryEntry {
    MemoryEntry {
        id: MemoryId::new(),
        key,
        content: MemoryContent::Text(content.to_string()),
        metadata: MemoryMetadata {
            source_agent: "test".to_string(),
            importance: 0.5,
            tags: vec!["test".to_string()],
            embedding: None,
        },
        created_at: chrono::Utc::now(),
        updated_at: chrono::Utc::now(),
    }
}
```

### 3. Fallback-Based Testing

Tests that don't need real infrastructure use `new_fallback()` constructors:

```rust
let store = ShortTermMemory::new_fallback(3600);
let store = PostgresMetadataStore::new_fallback();
let engine = LocalEngine::new_fallback();
```

This allows all tests to run without TiKV/Qdrant/PostgreSQL.

### 4. Async Tests

All async operations use `#[tokio::test]`:

```rust
#[tokio::test]
async fn test_fallback_read_write_delete() {
    let store = ShortTermMemory::new_fallback(3600);
    // ... async operations
}
```

### 5. Test Naming Convention

Pattern: `test_{unit}_{scenario}`

| Test Name | What It Tests |
|-----------|---------------|
| `test_encode_decode_task_key` | Key encoding roundtrip for task scope |
| `test_fallback_read_write_delete` | Full CRUD cycle on fallback store |
| `test_fallback_list_keys` | Prefix scanning on fallback store |
| `test_fallback_overwrite` | Overwriting an existing entry |
| `test_stored_entry_roundtrip` | Serialization roundtrip |
| `test_symbol_kind_roundtrip` | Enum <-> string roundtrip |
| `test_format_parse_health` | Health enum <-> string roundtrip |
| `test_fallback_repo_crud` | Repository CRUD on fallback |
| `test_fallback_symbol_search` | Symbol search on fallback |

### 6. Assertion Patterns

```rust
// Equality
assert_eq!(encoded, "memory:task:abc123:decisions");

// Boolean
assert!(result.is_some());
assert!(result.is_none());

// Pattern matching
assert!(matches!(result, ConflictResult::Conflicting { .. }));

// Panic on unexpected variant
if let MemoryContent::Text(text) = &read_entry.content {
    assert_eq!(text, "Use PostgreSQL for metadata");
} else {
    panic!("Expected Text content");
}
```

### 7. Feature-Gated Tests

Tests that require specific features use `#[cfg(feature = "...")]`:

```rust
#[cfg(feature = "storage")]
#[test]
fn test_format_parse_health() { ... }
```

### 8. gRPC Error Mapping Tests

The gRPC server tests verify each `EngineError` variant maps to the correct tonic `Code` (`crates/uc-grpc/src/server.rs:178-221`):

```rust
#[test]
fn error_mapping_search() {
    let status = to_status(EngineError::SearchError("test".into()));
    assert_eq!(status.code(), tonic::Code::Internal);
}

#[test]
fn error_mapping_rate_limited() {
    let status = to_status(EngineError::RateLimited(5));
    assert_eq!(status.code(), tonic::Code::ResourceExhausted);
}
```

---

## Python Test Patterns

Tests live in `tests/python/test_agent.py`.

### 1. pytest with Class Grouping

```python
class TestTask:
    """Tests for Task dataclass."""
    def test_task_defaults(self): ...
    def test_task_is_complete(self): ...

class TestOrchestrator:
    """Tests for Orchestrator."""
    def test_init_defaults(self): ...
```

### 2. unittest.mock for Engine/LLM

```python
from unittest.mock import AsyncMock, MagicMock, patch

def _make_engine(self):
    engine = MagicMock()
    engine.read_memory.return_value = None
    engine.write_memory.return_value = {"id": "mem-1", "content": "test"}
    return engine
```

### 3. Async Tests with pytest.mark.asyncio

```python
@pytest.mark.asyncio
async def test_register_worker(self):
    orch = Orchestrator()
    wi = WorkerInfo(id="w1", capabilities=["code"])
    await orch.register_worker(wi)
    assert "w1" in orch.workers
```

### 4. Integration-Style Tests

`TestOrchestratorWorkerIntegration` tests cross-component flows:

```python
class TestOrchestratorWorkerIntegration:
    @pytest.mark.asyncio
    async def test_decompose_and_assign(self):
        """Test the full flow: decompose task -> assign subtasks."""
        ...

    @pytest.mark.asyncio
    async def test_handle_subtask_result(self):
        """Test handling a successful subtask result."""
        ...

    @pytest.mark.asyncio
    async def test_handle_subtask_failure(self):
        """Test handling a failed subtask result."""
        ...
```

### 5. Python Assertion Patterns

```python
# Existence
assert task.id  # auto-generated UUID -- truthy check

# Equality
assert result.content == "Use PostgreSQL"

# None checks
assert result is None
assert result is not None

# Exception testing
with pytest.raises(ValueError, match="task_id"):
    mk.validate()

# Mock verification
engine.write_memory.assert_called_once()
call_kwargs = engine.write_memory.call_args
assert call_kwargs[1]["key_scope"] == "task"
```

---

## Naming Conventions Summary

| Item | Rust Convention | Python Convention |
|------|----------------|-------------------|
| Test function | `test_{unit}_{scenario}` | `test_{behavior}` |
| Test class | N/A (inline mod) | `Test{Component}` |
| Helper factory | `make_{thing}()` | `_make_{thing}()` (private) |
| Async test | `#[tokio::test]` | `@pytest.mark.asyncio` |
| Mock | N/A | `MagicMock` / `AsyncMock` |

---

## Required Patterns

1. **Every storage struct must have fallback-based tests** -- Use `new_fallback()` so tests run without infrastructure
2. **Every `EngineError` variant must have a mapping test** -- Both PyO3 and gRPC mappings should be verified
3. **Key encoding must have roundtrip tests** -- `encode_key` -> `decode_key` must be lossless
4. **Enum string conversions must have roundtrip tests** -- `format_health` / `parse_health`, `symbol_kind_to_str` / `parse_symbol_kind`

---

## Forbidden Patterns

1. **Never use `#[test]` on async functions** -- Always use `#[tokio::test]` for async
2. **Never use `unwrap()` in test assertions** -- Use `assert!`, `assert_eq!`, or `assert!(result.is_some())`
3. **Never connect to real infrastructure in unit tests** -- Use `new_fallback()` or dependency injection
4. **Never use `sleep()` in tests** -- If timing is needed, use tokio's time mocking
5. **Never leave `#[ignore]` tests without a comment explaining why** -- Disabled tests must document what infrastructure they need

---

## Code Review Checklist

- [ ] All new `EngineError` variants have both PyO3 and gRPC mapping tests
- [ ] Storage structs follow the fallback pattern (3 constructors, dual-path read/write)
- [ ] Error wrapping uses `.map_err()` with descriptive messages including the original error
- [ ] Test helpers use `make_` / `_make_` prefix conventions
- [ ] Feature-gated code has both `#[cfg(feature = "...")]` and `#[cfg(not(feature = "..."))]` paths
- [ ] No `unwrap()` in production code paths (test code is acceptable)
- [ ] Logging uses the correct level (info/warn/debug) per the logging guidelines
