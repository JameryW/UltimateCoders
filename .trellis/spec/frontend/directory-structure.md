# Directory Structure

> How the Python agent layer is organized.

---

## Overview

The Python code lives under `python/ultimate_coders/`. It provides the ergonomic Python interface on top of the Rust engine, including the agent orchestration layer, memory wrappers, and search types.

---

## Package Layout

```
python/ultimate_coders/
    __init__.py           # Top-level exports (Engine, Orchestrator, Worker, Memory types)
    config.py             # Configuration dataclasses (EngineConfig, StorageConfig, etc.)
    engine.py             # Engine factory -- switches between local/gRPC mode

    agent/
        __init__.py       # Re-exports all agent types
        types.py          # Data classes (Task, Subtask, WorkerInfo, enums)
        orchestrator.py   # Orchestrator -- task decomposition + worker coordination
        worker.py         # Worker -- subtask execution with LLM + tools
        llm.py            # LLM client abstraction (Anthropic API)
        conflict.py       # Conflict detection/resolution (Python-side)
        rate_limiter.py   # Rate limiting + circuit breaker (Python-side)
        sandbox.py        # Sandbox agent execution (Claude Code / Codex adapters)

    memory/
        __init__.py       # Re-exports MemoryKey, MemoryEntry, ShortTermMemory, LongTermMemory
        memory.py         # MemoryKey, MemoryEntry, ShortTermMemory, LongTermMemory wrappers

    search/
        __init__.py       # Re-exports SearchQuery, SearchResult, SearchResultItem
        query.py          # SearchQuery builder pattern
        result.py         # SearchResultItem, SearchResult dataclasses
```

---

## Module Organization Rules

1. **Types go in `types.py`** -- All data classes and enums for a domain live in a single `types.py` file (e.g., `agent/types.py` has `Task`, `Subtask`, `WorkerInfo`, `TaskStatus`, `SubtaskStatus`, `ChangeType`)

2. **Re-exports through `__init__.py`** -- Each sub-package's `__init__.py` re-exports the public API so consumers can import from the package root:
   ```python
   from ultimate_coders.agent.types import Task  # Direct path
   from ultimate_coders.agent import Task         # Via __init__.py re-export
   ```

3. **Wrappers in the same module as the types they wrap** -- `MemoryKey` and `MemoryEntry` are in `memory/memory.py` alongside the `ShortTermMemory` and `LongTermMemory` wrapper classes

4. **Builder classes alongside their result types** -- `SearchQuery` (builder) is in `search/query.py`, `SearchResult`/`SearchResultItem` are in `search/result.py`

5. **Config in a dedicated module** -- All configuration dataclasses (`EngineConfig`, `StorageConfig`, `NatsConfig`, `LlmConfig`, `Config`) live in `config.py` at the package root

---

## Test Layout

```
tests/python/
    test_agent.py         # All agent layer tests (types, LLM, Orchestrator, Worker, integration)
```

Tests are grouped by class (`TestTask`, `TestOrchestrator`, `TestWorker`, `TestOrchestratorWorkerIntegration`) rather than by file.

---

## Naming Conventions

| Item | Convention | Example |
|------|-----------|---------|
| Package directory | `snake_case` | `agent/`, `memory/`, `search/` |
| Module file | `snake_case` | `orchestrator.py`, `rate_limiter.py` |
| Class | `PascalCase` | `Orchestrator`, `ShortTermMemory`, `SearchQuery` |
| Dataclass | `PascalCase` | `Task`, `SubtaskResult`, `WorkerInfo` |
| Enum | `PascalCase` | `TaskStatus`, `SubtaskStatus`, `ChangeType` |
| Enum member | `UPPER_SNAKE_CASE` | `IN_PROGRESS`, `COMPLETED`, `FAILED` |
| Private field | `_` prefix | `_query`, `_modes`, `_repo_ids` (in SearchQuery builder) |
| Helper factory (test) | `_make_` prefix | `_make_engine()` |
| System prompt constant | `_UPPER_SNAKE_CASE` | `_DECOMPOSE_SYSTEM_PROMPT`, `_WORKER_SYSTEM_PROMPT` |
