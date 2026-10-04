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
- It enables `X | Y` union syntax, which is the form this codebase actually uses
- It prevents circular import issues with type hints

**Real examples**: `python/ultimate_coders/agent/types.py`, `python/ultimate_coders/memory/memory.py`, `python/ultimate_coders/search/query.py`, `python/ultimate_coders/config.py`

---

## Type Import Conventions

Annotations use the **PEP 585/604 builtins**, not the `typing` spellings.
`typing` is still imported where a builtin has no equivalent (`Any`,
`Callable`, `TypeVar`).

Measured on the live tree, 2026-10-04 (`rg -o` over the `python/` tree):

- Optional values: `X | None` (405 uses) -- NOT `Optional[X]` (3 uses).
- Lists: `list[X]` (306 uses) -- NOT `List[X]` (0 uses).
- Dicts: `dict[str, Any]` (148 uses of bare `dict`) -- NOT `Dict[...]` (0 uses).
- Tuples: `tuple[...]` -- NOT `Tuple[...]` (0 uses).
- `Any` for engine handles and unstructured payloads (see below).

> **Warning** Do not "fix" annotations into `Optional[X]` / `List[X]` /
> `Dict[str, Any]`. This document previously claimed the opposite; the
> measurement above is what the code does. Rewriting the 405 to match the 3
> would fight the linter and churn every module.
>
> **But the 3 are load-bearing, not legacy.** Two `Optional[X]` sites carry
> `# noqa: UP045`, and they are the exception for a reason that has nothing to
> do with taste: FastAPI evaluates endpoint annotations at *runtime* through
> `typing.get_type_hints`, and `X | None` does not survive that on Python 3.9,
> which this project still supports (`requires-python = ">=3.9"`, CI matrix
> `["3.9", "3.12"]`). `from __future__ import annotations` only defers the
> annotation at definition time; it does not help once something evaluates it.
>
> Measured on CPython 3.9.22 (2026-10-04), `from __future__ import annotations`
> at the top of the probe file:
>
> ```
> opt   get_type_hints -> typing.Optional[str]
> pep   get_type_hints -> TypeError: unsupported operand type(s) for |: 'type' and 'NoneType'
> ```
>
> **Rule: never put a PEP 604 union in a signature that a framework evaluates
> at runtime while Python 3.9 is supported.** That means FastAPI route
> parameters and Pydantic model fields. Everywhere else -- ordinary methods,
> helpers, `->` returns -- `X | None` is correct and preferred. All 21 routes
> in `python/ultimate_coders/dashboard/app.py` were scanned on 2026-10-04 and
> none carries a PEP 604 union; the tree is currently safe on 3.9 *because*
> `events_api`'s `task_id` kept the `Optional[str]` spelling and suppressed the
> lint that would "fix" it. Removing that suppression is a Python 3.9
> regression, not a cleanup.

```python
from __future__ import annotations

from typing import Any, Callable


async def read(key: str, task_id: str, *, limit: int | None = None) -> dict[str, Any] | None:
    ...
```

---

## The `Any` Pattern for Engine

The `engine` parameter is typed as `Any` throughout the codebase because it can be either:
- A `PyEngine` (Rust extension object)
- A `MagicMock` (in tests)
- `None` (when engine is unavailable)

**Real examples**:
- `python/ultimate_coders/memory/memory.py`: `class ShortTermMemory:` -- `engine: Any`
- `python/ultimate_coders/memory/memory.py`: `class LongTermMemory:` -- `engine: Any`
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

1. **Using `Optional[X]` / `List[X]` / `Dict[...]`** -- these are the
   rejected spellings here, not the required ones. The codebase is
   consistently PEP 585/604 (measured 2026-10-04: 405 `X | None` against
   3 `Optional[X]`, 306 `list[` against 0 `List[`, 148 `dict` against 0
   `Dict[`). ruff's UP006/UP045 rules rewrite toward the builtin form.

   **The one place `Optional[X]` is still required** is a signature a
   framework evaluates at runtime -- a FastAPI route parameter, a Pydantic
   field -- while Python 3.9 is supported. `X | None` raises
   `TypeError: unsupported operand type(s) for |` there on 3.9 even under
   `from __future__ import annotations`. The two `# noqa: UP045` sites are
   that exception. Do not "clean them up"; see the warning above.

2. **Dropping the element/key type entirely** -- the distinction that
   matters is bare `list` / `dict` in place of `list[X]` / `dict[str, Any]`,
   not `list` in place of `List`. Annotate the parameters even when you use
   the builtin spelling.

3. **Typing a decoded third-party wire payload as a `@dataclass`** -- see
   the gotcha in `.trellis/spec/backend/inference-infra-spec.md`. The
   dataclass rule in `component-guidelines.md` governs UC's own domain and
   config types, not JSON decoded from an external service, which is probed
   defensively with `.get()` against a versioned contract.

4. **Forgetting `from __future__ import annotations`** -- This import is required at the top of every Python file. Without it, forward references and deferred evaluation will fail.

5. **Typing engine as a concrete class** -- The engine must be typed as `Any` (or `Optional[Any]`) because it can be a Rust extension object, a mock, or None.

6. **Using `field(default=ClassName())` for dataclass defaults** -- This creates a single shared instance. Always use `field(default_factory=ClassName)`.
