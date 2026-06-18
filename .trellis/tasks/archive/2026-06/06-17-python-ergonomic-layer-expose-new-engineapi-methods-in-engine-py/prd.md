# Python Ergonomic Layer: Expose New EngineApi Methods

## Goal

Python `Engine` class (engine.py) 缺少 Rust/PyO3 层已暴露的新方法。需要补全 Python ergonomic wrapper，让 Python 用户能直接调用 batch_write_memory、list_repos、search_stream 以及 task 方法。

## Gap Analysis

Rust PyO3 (_uc_core.PyEngine) 有但 Python Engine 类没有的方法：

**Batch/List/Stream:**
- `batch_write_memory(requests)` → Rust 已暴露
- `list_repos()` → Rust 已暴露
- `search_stream(query)` → Rust 已暴露

**Task methods (PR #63 pending merge):**
- `submit_task(description, project_id=None)` → Rust 已暴露
- `get_task(task_id)` → Rust 已暴露
- `list_tasks()` → Rust 已暴露
- `pause_task(task_id)` → Rust 已暴露
- `resume_task(task_id)` → Rust 已暴露

**Async variants of all above**

## Requirements

### 1. Batch/List/Stream methods
在 engine.py 的 Engine 类中添加：

```python
def batch_write_memory(self, requests: list[dict]) -> list:
    """Write multiple memory entries in a single call."""
    return self._try_grpc_with_fallback("batch_write_memory", requests)

def list_repos(self) -> list:
    """List all indexed repositories."""
    return self._try_grpc_with_fallback("list_repos")

def search_stream(self, query) -> list:
    """Stream search results, collected into a list."""
    py_query = self._convert_search_query(query)
    return self._try_grpc_with_fallback("search_stream", py_query)
```

### 2. Task methods
```python
def submit_task(self, description: str, project_id: str | None = None) -> object:
    """Submit a new task for orchestration."""
    return self._try_grpc_with_fallback(
        "submit_task", description, project_id or ""
    )

def get_task(self, task_id: str) -> object:
    """Get a task by ID."""
    return self._try_grpc_with_fallback("get_task", task_id)

def list_tasks(self) -> list:
    """List all tasks."""
    return self._try_grpc_with_fallback("list_tasks")

def pause_task(self, task_id: str) -> object:
    """Pause a running task."""
    return self._try_grpc_with_fallback("pause_task", task_id)

def resume_task(self, task_id: str) -> object:
    """Resume a paused task."""
    return self._try_grpc_with_fallback("resume_task", task_id)
```

### 3. Async variants
For each new method, add `_async` variant following the existing pattern.

## Acceptance Criteria

* [x] batch_write_memory, list_repos, search_stream in Engine class
* [x] submit_task, get_task, list_tasks, pause_task, resume_task in Engine class
* [x] All new methods have async variants
* [x] All methods use _try_grpc_with_fallback pattern
* [x] Python import works: `from ultimate_coders import Engine`

## Out of Scope

* watch_task async (stream)
* New Python type wrappers (PyTask fields are accessed as attributes)
* Integration tests (requires running server)
