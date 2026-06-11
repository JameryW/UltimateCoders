# Type Safety

> Type annotation conventions, Optional/List/Dict usage, and engine typing patterns.

---

## Overview

The Python layer uses strict type annotations throughout, enabled by `from __future__ import annotations` at the top of every file. The codebase targets Python 3.10+ and uses modern typing syntax.

---

## `from __future__ import annotations`

**Every Python file** starts with this import to enable PEP 604 deferred annotation evaluation:

```python
from __future__ import annotations
```

This is mandatory because:
- It allows forward references without string quoting
- It enables `X | Y` union syntax (though the codebase currently uses `Optional[X]`)
- It prevents circular import issues with type hints

**Real examples**: `python/ultimate_coders/agent/types.py:1`, `python/ultimate_coders/memory/memory.py:1`, `python/ultimate_coders/search/query.py:1`, `python/ultimate_coders/config.py:1`

---

## Type Import Conventions

The codebase uses `typing` module imports (not `builtins`):

```python
from typing import Any, Dict, List, Optional
```

| Type | Import | Usage |
|------|--------|-------|
| Optional values | `Optional[X]` | `Optional[str] = None`, `Optional[datetime] = None` |
| Lists | `List[X]` | `List[Subtask]`, `List[str]`, `List[SearchResultItem]` |
| Dicts | `Dict[str, Any]` | `Dict[str, Any]` for unstructured data |
| Any | `Any` | Engine instances (see below) |
| Union | `Optional[X]` (not `X | None`) | Consistent with existing codebase |

---

## The `Any` Pattern for Engine

The `engine` parameter is typed as `Any` throughout the codebase because it can be either:
- A `PyEngine` (Rust extension object)
- A `MagicMock` (in tests)
- `None` (when engine is unavailable)

**Real examples**:
- `python/ultimate_coders/memory/memory.py:149`: `class ShortTermMemory:` -- `engine: Any`
- `python/ultimate_coders/memory/memory.py:257`: `class LongTermMemory:` -- `engine: Any`
- `python/ultimate_coders/agent/orchestrator.py:95`: `engine: Optional[Any]`

The alternative of using a Protocol/ABC was considered but not adopted because the Rust extension object does not implement Python ABCs, and the mock would need to be cast.

---

## Dataclass Field Typing

### Mutable Defaults

Use `field(default_factory=...)` for mutable types:

```python
# Correct
tags: List[str] = field(default_factory=list)
subtasks: List[Subtask] = field(default_factory=list)
config: EngineConfig = field(default_factory=EngineConfig)

# Wrong -- shared mutable default
tags: List[str] = []
subtasks: List[Subtask] = []
```

### Auto-generated IDs and Timestamps

Use lambda factories for unique values:

```python
id: str = field(default_factory=lambda: str(uuid.uuid4()))
created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
```

### Optional Fields

Use `Optional[T] = None` for nullable fields:

```python
result: Optional[str] = None
embedding: Optional[List[float]] = None
grpc_endpoint: Optional[str] = None
```

---

## Method Return Type Annotations

All public methods include return type annotations:

```python
def read(self, key: str, task_id: str, ...) -> Optional[MemoryEntry]: ...
def write(self, key: str, content: str, ...) -> MemoryEntry: ...
def search(self, query: SearchQuery) -> SearchResult: ...
def submit_task(self, description: str, ...) -> Task: ...
def register_worker(self, worker_info: WorkerInfo) -> None: ...
```

Private helper methods also include return types:

```python
def _to_entry(self, raw: Any) -> MemoryEntry: ...
def _aggregate_results(self, task: Task) -> str: ...
def _handle_result(self, task: Task, subtask: Subtask, result: SubtaskResult) -> None: ...
```

---

## Enum Value Types

Enums use string values for JSON serialization compatibility:

```python
class TaskStatus(Enum):
    CREATED = "created"       # Not auto() or int values
    PLANNING = "planning"
    IN_PROGRESS = "in_progress"
```

Comparison is done via the enum member, not the string value:

```python
# Correct
if task.status == TaskStatus.COMPLETED: ...

# Wrong (bypasses type checking)
if task.status.value == "completed": ...
```

---

## Type Narrowing Patterns

The codebase uses `isinstance` checks for type narrowing when engine returns can be either dict or Rust objects:

```python
def _to_entry(self, raw: Any) -> MemoryEntry:
    if isinstance(raw, dict):
        return MemoryEntry.from_dict(raw)
    try:
        return MemoryEntry.from_rust(raw)
    except Exception:
        return MemoryEntry(content=str(raw))
```

---

## Common Mistakes

1. **Using `X | None` instead of `Optional[X]`** -- The codebase consistently uses `Optional[X]`. While PEP 604 allows `X | None`, mixing styles creates inconsistency.

2. **Using `dict` instead of `Dict[str, Any]`** -- Always specify the key and value types for dicts used as data structures.

3. **Using `list` instead of `List[X]`** -- Always specify the element type for lists used as data structures.

4. **Forgetting `from __future__ import annotations`** -- This import is required at the top of every Python file. Without it, forward references and deferred evaluation will fail.

5. **Typing engine as a concrete class** -- The engine must be typed as `Any` (or `Optional[Any]`) because it can be a Rust extension object, a mock, or None.

6. **Using `field(default=ClassName())` for dataclass defaults** -- This creates a single shared instance. Always use `field(default_factory=ClassName)`.
