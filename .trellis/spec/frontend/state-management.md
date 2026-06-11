# State Management

> How state is managed in the Python agent layer -- task state machines, in-memory state, computed properties, and memory persistence.

---

## Overview

State in the Python layer is managed through:

1. **Task/Subtask state machines** -- Enums that track lifecycle transitions
2. **In-memory state on the Orchestrator** -- Dictionaries of tasks and workers
3. **Computed properties on dataclasses** -- Derived state from subtask status
4. **Memory persistence** -- State is persisted to engine memory for durability

---

## Task State Machine

### TaskStatus

`TaskStatus` (`python/ultimate_coders/agent/types.py:12-19`):

```
CREATED --> PLANNING --> IN_PROGRESS --> COMPLETED
                                     \--> FAILED
                                     \--> PAUSED
```

Transition triggers:

| From | To | Trigger | Method |
|------|----|---------|--------|
| CREATED | PLANNING | Task submitted to orchestrator | `submit_task()` |
| PLANNING | IN_PROGRESS | Decomposition complete, subtasks created | `submit_task()` |
| PLANNING | FAILED | Decomposition failed | `submit_task()` |
| IN_PROGRESS | COMPLETED | All subtasks completed | `handle_subtask_result()` |
| IN_PROGRESS | FAILED | Any subtask failed | `handle_subtask_result()` |

### SubtaskStatus

`SubtaskStatus` (`python/ultimate_coders/agent/types.py:22-29`):

```
PENDING --> ASSIGNED --> IN_PROGRESS --> COMPLETED
                                 \--> FAILED
                                 \--> CONFLICTED
```

Transition triggers:

| From | To | Trigger | Method |
|------|----|---------|--------|
| PENDING | ASSIGNED | Assigned to a worker | `assign_subtask()` |
| ASSIGNED | IN_PROGRESS | Worker starts execution | Worker callback |
| IN_PROGRESS | COMPLETED | Worker reports success | `handle_subtask_result()` |
| IN_PROGRESS | FAILED | Worker reports failure | `handle_subtask_result()` |
| IN_PROGRESS | CONFLICTED | Conflict detected | Conflict resolution |

---

## In-Memory State on the Orchestrator

The Orchestrator maintains two primary dictionaries (`python/ultimate_coders/agent/orchestrator.py:117-118`):

```python
self.workers: Dict[str, WorkerInfo] = {}
self.tasks: Dict[str, Task] = {}
```

### Worker State

- **Registration**: `register_worker(wi)` adds `WorkerInfo` to `self.workers`
- **Load tracking**: `current_load` increments on assignment, decrements on result
- **Availability**: `WorkerInfo.is_available` checks `current_load < max_capacity`

### Task State

- **Creation**: `submit_task()` creates a `Task` and adds it to `self.tasks`
- **Lookup**: `get_task_status(task_id)` returns from `self.tasks`
- **Update**: State transitions modify the `Task` object in-place within the dict

---

## Computed Properties

Derived state is computed on-the-fly via `@property` on dataclasses:

### Task Properties (`python/ultimate_coders/agent/types.py:103-123`)

```python
@property
def is_complete(self) -> bool:
    """All subtasks completed successfully."""
    return len(self.subtasks) > 0 and all(st.is_complete for st in self.subtasks)

@property
def has_failed(self) -> bool:
    """Any subtask has failed."""
    return any(st.is_failed for st in self.subtasks)

@property
def ready_subtasks(self) -> List[Subtask]:
    """Pending subtasks with all dependencies met."""
    completed_ids = {st.id for st in self.subtasks if st.is_complete}
    return [st for st in self.subtasks if st.is_ready and all(dep in completed_ids for dep in st.depends_on)]
```

### Subtask Properties (`python/ultimate_coders/agent/types.py:71-84`)

```python
@property
def is_ready(self) -> bool:
    return self.status == SubtaskStatus.PENDING

@property
def is_complete(self) -> bool:
    return self.status == SubtaskStatus.COMPLETED

@property
def is_failed(self) -> bool:
    return self.status == SubtaskStatus.FAILED
```

### WorkerInfo Properties (`python/ultimate_coders/agent/types.py:135-138`)

```python
@property
def is_available(self) -> bool:
    return self.current_load < self.max_capacity
```

---

## Memory Persistence

State is persisted to engine memory (if available) at key lifecycle transitions:

### Task Creation (`python/ultimate_coders/agent/orchestrator.py:149-161`)

```python
if self.engine is not None:
    try:
        self.engine.write_memory(
            key_scope="task",
            key="task_definition",
            content=description,
            content_type="text",
            source_agent="orchestrator",
            task_id=task.id,
            project_id=project_id,
        )
    except Exception:
        logger.warning("Failed to write task to memory", exc_info=True)
```

### Subtask Assignment (`python/ultimate_coders/agent/orchestrator.py:256-273`)

Writes assignment details to memory so they can be recovered on orchestrator restart.

### Subtask Result (`python/ultimate_coders/agent/orchestrator.py:329-340`)

Writes result details including modified files and summary.

### Persistence Rules

1. **Always check `if self.engine is not None`** before calling engine methods
2. **Always wrap engine calls in try/except** -- memory persistence is best-effort
3. **Log failures with `logger.warning(..., exc_info=True)`** but do not propagate
4. **Primary state is in-memory** -- the `self.tasks` / `self.workers` dicts are the source of truth during execution; memory is the durability layer

---

## Task Completion Detection

The Orchestrator checks task completion in `handle_subtask_result()`:

1. Update the subtask status (COMPLETED or FAILED)
2. Check `task.is_complete` -- all subtasks completed
3. Check `task.has_failed` -- any subtask failed
4. If complete, set `task.status = TaskStatus.COMPLETED` and call `_aggregate_results()`
5. If failed, set `task.status = TaskStatus.FAILED`

---

## Common Mistakes

1. **Mutating state without updating timestamps** -- Always call `task.update_timestamp()` after modifying task state

2. **Forgetting to decrement worker load** -- When a subtask completes or fails, the worker's `current_load` must be decremented in `handle_subtask_result()`

3. **Checking `task.is_complete` on a task with no subtasks** -- `is_complete` returns `False` when `subtasks` is empty, even though the task is technically created. This is intentional -- a task with no subtasks is never "complete".

4. **Not handling the PAUSED state** -- `TaskStatus.PAUSED` is defined but not actively used in the current orchestrator. It exists for future checkpoint/resume support.
