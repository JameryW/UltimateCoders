# Component Guidelines

> Dataclass, Enum, Builder, and Adapter patterns used in the Python agent layer.

---

## Overview

The Python layer uses four primary structural patterns:

1. **Dataclass pattern** -- All data types use `@dataclass` (never TypedDict)
2. **Enum pattern** -- Status/type enums inherit from `Enum` with string values
3. **Builder pattern** -- `SearchQuery` uses fluent builder methods
4. **Adapter pattern** -- `Engine` / `MemoryEntry` wrap Rust or dict sources

---

## Dataclass Pattern

All data types use `@dataclass` with `field(default_factory=...)` for mutable defaults.

### Examples

**Task** (`python/ultimate_coders/agent/types.py:88-98`):
```python
@dataclass
class Task:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    description: str = ""
    project_id: str = ""
    status: TaskStatus = TaskStatus.CREATED
    subtasks: List[Subtask] = field(default_factory=list)
    result: Optional[str] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
```

**OrchestratorConfig** (`python/ultimate_coders/agent/types.py:142-147`):
```python
@dataclass
class OrchestratorConfig:
    max_subtasks: int = 10
    max_retries: int = 3
    heartbeat_timeout_seconds: int = 60
```

**MemoryEntry** (`python/ultimate_coders/memory/memory.py:46-59`):
```python
@dataclass
class MemoryEntry:
    id: str = ""
    key: MemoryKey = field(default_factory=lambda: MemoryKey(scope="global", key=""))
    content: str = ""
    content_type: str = "text"
    source_agent: str = ""
    importance: float = 0.5
    tags: List[str] = field(default_factory=list)
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
```

**Config** (`python/ultimate_coders/config.py:49-54`):
```python
@dataclass
class Config:
    engine: EngineConfig = field(default_factory=EngineConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    nats: NatsConfig = field(default_factory=NatsConfig)
    llm: LlmConfig = field(default_factory=LlmConfig)
```

### Rules

- **Always use `field(default_factory=list)`** for `List` fields (never `= []`)
- **Always use `field(default_factory=lambda: ...)`** for auto-generated IDs and timestamps
- **Use `field(default_factory=ClassName)`** when the default is another dataclass (e.g., `field(default_factory=EngineConfig)`)
- **Use `Optional[T] = None`** for nullable fields
- **Never use TypedDict** -- not used anywhere in the codebase

---

## Enum Pattern

Status and type enums use `Enum` with string values. This allows JSON serialization and comparison with API responses.

### Examples

**TaskStatus** (`python/ultimate_coders/agent/types.py:12-19`):
```python
class TaskStatus(Enum):
    CREATED = "created"
    PLANNING = "planning"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    PAUSED = "paused"
```

**SubtaskStatus** (`python/ultimate_coders/agent/types.py:22-29`):
```python
class SubtaskStatus(Enum):
    PENDING = "pending"
    ASSIGNED = "assigned"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    CONFLICTED = "conflicted"
```

**ChangeType** (`python/ultimate_coders/agent/types.py:32-36`):
```python
class ChangeType(Enum):
    CREATED = "created"
    MODIFIED = "modified"
    DELETED = "deleted"
```

### Rules

- Enum members use `UPPER_SNAKE_CASE` names
- Enum values are lowercase strings (e.g., `"in_progress"`, not `"IN_PROGRESS"`)
- This matches the Rust-side string conventions used in serialization

---

## Builder Pattern

`SearchQuery` uses a fluent builder with method chaining.

### Example (`python/ultimate_coders/search/query.py:8-51`):

```python
class SearchQuery:
    def __init__(self, query: str):
        self._query = query
        self._modes: List[str] = ["hybrid"]
        self._repo_ids: List[str] = []
        self._languages: List[str] = []
        self._path_patterns: List[str] = []
        self._max_results: int = 10

    def in_repos(self, repo_ids: List[str]) -> SearchQuery:
        self._repo_ids = repo_ids
        return self

    def in_languages(self, languages: List[str]) -> SearchQuery:
        self._languages = languages
        return self

    def with_modes(self, modes: List[str]) -> SearchQuery:
        self._modes = modes
        return self

    def limit(self, max_results: int) -> SearchQuery:
        self._max_results = max_results
        return self

    def to_dict(self) -> dict:
        return { ... }
```

**Usage**:
```python
query = (SearchQuery("authentication logic")
         .in_repos(["my-backend", "my-frontend"])
         .in_languages(["python", "rust"])
         .with_modes(["semantic", "ast"])
         .limit(20))
```

### Rules

- Builder fields use `_` prefix (private)
- Each builder method returns `self` for chaining
- Terminal method is `to_dict()` which produces the dict for the Rust engine
- Default values are set in `__init__`

---

## Adapter Pattern

### Engine Adapter

The `Engine` class (`python/ultimate_coders/engine.py:18-55`) wraps `PyEngine` from the Rust extension:

```python
class Engine:
    def __init__(self, mode: str = "local", grpc_endpoint: Optional[str] = None):
        if PyEngine is None:
            raise ImportError("Rust extension not built. Run `maturin develop` first.")
        self._mode = mode
        self._engine = PyEngine(mode=mode, grpc_endpoint=grpc_endpoint)
```

The Engine converts between Python types and Rust types (e.g., `SearchQuery` -> `PySearchQuery`).

### Memory Wrappers

`ShortTermMemory` and `LongTermMemory` (`python/ultimate_coders/memory/memory.py:147-415`) delegate to the Engine and convert results via `_to_entry()`:

```python
class ShortTermMemory:
    def __init__(self, engine: Any):
        self.engine = engine

    def read(self, key: str, task_id: str, ...) -> Optional[MemoryEntry]:
        raw = self.engine.read_memory(key_scope="task", key=key, task_id=task_id, ...)
        if raw is None:
            return None
        return self._to_entry(raw)
```

### from_rust / from_dict Dual Construction

`MemoryEntry` supports construction from both Rust extension objects and plain dicts (`python/ultimate_coders/memory/memory.py:62-144`):

```python
@classmethod
def from_rust(cls, raw: Any) -> MemoryEntry:
    """Create a MemoryEntry from a PyMemoryEntry (Rust extension)."""
    content_obj = getattr(raw, "content", None)
    # ... extract fields using getattr with defaults

@classmethod
def from_dict(cls, data: Dict[str, Any]) -> MemoryEntry:
    """Create a MemoryEntry from a dict."""
    key_data = data.get("key", {})
    # ... extract fields using dict.get with defaults
```

The `_to_entry()` helper tries both conversion paths:

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

## Computed Properties

Dataclasses use `@property` for derived state (not methods):

**Task** (`python/ultimate_coders/agent/types.py:103-123`):
```python
@property
def is_complete(self) -> bool:
    return len(self.subtasks) > 0 and all(st.is_complete for st in self.subtasks)

@property
def has_failed(self) -> bool:
    return any(st.is_failed for st in self.subtasks)

@property
def ready_subtasks(self) -> List[Subtask]:
    completed_ids = {st.id for st in self.subtasks if st.is_complete}
    return [st for st in self.subtasks if st.is_ready and all(dep in completed_ids for dep in st.depends_on)]
```

**SearchResultItem** (`python/ultimate_coders/search/result.py:23-25`):
```python
@property
def location(self) -> str:
    return f"{self.repo_id}:{self.file_path}:{self.start_line}"
```

---

## Common Mistakes

1. **Using `= []` as a default** -- Python mutable default arguments are shared across instances. Always use `field(default_factory=list)`.

2. **Using TypedDict instead of dataclass** -- The codebase exclusively uses `@dataclass`. TypedDict is not used anywhere.

3. **Forgetting `from __future__ import annotations`** -- Every Python file starts with this import to enable PEP 604 deferred annotation evaluation.

4. **Not handling `None` from engine operations** -- Engine `read_memory` returns `None` when the key is not found. Always check for `None` before converting.
