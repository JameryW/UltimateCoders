# Quality Guidelines

> pytest patterns, mocking conventions, test organization, and review checklist.

---

## Overview

Python tests live in `tests/python/test_agent.py` using `pytest` with `unittest.mock`. Tests are organized by class grouping, with integration tests in a dedicated class. All tests mock the engine (they do not test the Rust extension directly).

---

## Test Organization

Tests are grouped by class rather than by file. The current single-file structure groups logically related tests:

```
tests/python/test_agent.py
    class TestTask          -- Task dataclass tests
    class TestSubtask       -- Subtask dataclass tests
    class TestWorkerInfo    -- WorkerInfo dataclass tests
    class TestLLMClient     -- LLM client tests
    class TestLLMResponse   -- LLM response tests
    class TestToolDefinition -- Tool definition tests
    class TestMemoryKey     -- MemoryKey validation tests
    class TestMemoryEntry   -- MemoryEntry from_dict tests
    class TestShortTermMemory -- Short-term memory wrapper tests
    class TestLongTermMemory -- Long-term memory wrapper tests
    class TestOrchestrator  -- Orchestrator tests
    class TestWorker        -- Worker tests
    class TestOrchestratorWorkerIntegration -- Cross-component flow tests
```

When adding new components, add a new `Test{Component}` class within the same file. Split into separate files only when the test file exceeds ~500 lines.

---

## Helper Factory Pattern

Each test class uses a `_make_engine()` helper that returns a pre-configured mock:

```python
class TestShortTermMemory:
    def _make_engine(self):
        engine = MagicMock()
        engine.read_memory.return_value = None
        engine.write_memory.return_value = {"id": "mem-1", "content": "test"}
        return engine
```

```python
class TestLongTermMemory:
    def _make_engine(self):
        engine = MagicMock()
        engine.read_memory.return_value = None
        engine.write_memory.return_value = {"id": "mem-1", "content": "test"}
        engine.search_memory.return_value = []
        return engine
```

The factory method returns a `MagicMock` with sensible defaults for the test class's domain.

---

## Mocking Conventions

### MagicMock vs AsyncMock

- **`MagicMock`** -- For synchronous engine methods (read_memory, write_memory, delete_memory)
- **`AsyncMock`** -- For async Orchestrator/Worker methods when needed

```python
from unittest.mock import AsyncMock, MagicMock, patch
```

### Mock Return Value Setup

Set return values on the mock before creating the object under test:

```python
engine = MagicMock()
engine.read_memory.return_value = None           # Not found case
engine.write_memory.return_value = {"id": "mem-1", "content": "test"}
engine.search_memory.return_value = []
```

### Verifying Mock Calls

Use `assert_called_once()` and inspect call arguments:

```python
engine.write_memory.assert_called_once()
call_kwargs = engine.write_memory.call_args
assert call_kwargs[1]["key_scope"] == "task"
assert call_kwargs[1]["key"] == "decisions"
```

`call_args[1]` accesses keyword arguments (index 0 is positional args).

---

## Async Tests

All async tests use `@pytest.mark.asyncio`:

```python
@pytest.mark.asyncio
async def test_register_worker(self):
    orch = Orchestrator()
    wi = WorkerInfo(id="w1", capabilities=["code"])
    await orch.register_worker(wi)
    assert "w1" in orch.workers
```

---

## Assertion Patterns

| Pattern | Use Case |
|---------|----------|
| `assert result is None` | Engine returns None |
| `assert result is not None` | Engine returns a value |
| `assert result.content == "..."` | Exact content match |
| `assert task.id` | Truthy check (auto-generated UUID) |
| `pytest.raises(ValueError, match="...")` | Expected exception with message pattern |
| `engine.write_memory.assert_called_once()` | Mock call verification |

---

## Integration-Style Tests

`TestOrchestratorWorkerIntegration` tests cross-component flows without external services:

```python
class TestOrchestratorWorkerIntegration:
    @pytest.mark.asyncio
    async def test_decompose_and_assign(self):
        """Test the full flow: decompose task -> assign subtasks."""
        orch = Orchestrator()
        await orch.register_worker(WorkerInfo(id="w1", capabilities=["code"]))

        # Parse decomposition manually (skip LLM)
        response = LLMResponse(text=json.dumps([...]))
        subtasks = orch._parse_decomposition(response, "task-1")

        # Assign the first subtask
        assigned = await orch.assign_subtask(subtasks[0])
        assert assigned == "w1"
        assert subtasks[0].status == SubtaskStatus.ASSIGNED

    @pytest.mark.asyncio
    async def test_handle_subtask_result(self):
        """Test handling a successful subtask result."""
        # ... setup task + worker
        result = SubtaskResult(subtask_id="s1", worker_id="w1", summary="Done", success=True)
        await orch.handle_subtask_result(result)
        assert st.status == SubtaskStatus.COMPLETED
        assert task.is_complete
```

These tests mock the LLM and engine but exercise real Orchestrator logic.

---

## Exception Testing

Use `pytest.raises` with `match` for expected exceptions:

```python
# ValueError with message pattern
with pytest.raises(ValueError, match="task_id"):
    mk.validate()

# RuntimeError with message pattern
with pytest.raises(RuntimeError, match="Failed to parse"):
    orch._parse_decomposition(response, "task-1")
```

---

## Required Patterns

1. **Every new dataclass must have a test class** -- Cover defaults, computed properties, and validation
2. **Every new wrapper class must have a `_make_engine()` helper** -- Mock engine with sensible defaults
3. **State transitions must be tested** -- Verify the target status after each transition
4. **Integration tests must cover cross-component flows** -- At minimum: submit -> assign -> result

---

## Forbidden Patterns

1. **Never test the Rust extension directly** -- Mock the engine; Rust extension tests are in Rust
2. **Never use `assert True` or `assert not False`** -- Always assert a specific condition
3. **Never use `try/except` in tests to catch expected exceptions** -- Use `pytest.raises`
4. **Never use `sleep()` in tests** -- If timing is needed, mock `datetime.now()`
5. **Never import from `_uc_core` in tests** -- The Rust extension may not be built during test runs

---

## Code Review Checklist

- [ ] Every new dataclass/enum has a corresponding `Test*` class
- [ ] Helper factories use `_make_` prefix and return properly configured mocks
- [ ] Async tests use `@pytest.mark.asyncio`
- [ ] Mock assertions verify keyword arguments, not just that the method was called
- [ ] Exception tests use `pytest.raises` with `match` parameter
- [ ] Integration tests cover the full lifecycle (submit -> assign -> result -> completion)
- [ ] No direct Rust extension imports in test code