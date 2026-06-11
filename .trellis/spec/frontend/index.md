# Python Agent Layer Guidelines

> Conventions for the Python agent layer -- Orchestrator, Worker, Memory, and Search.

---

## Overview

The "frontend" in this project is the **Python Agent Layer** -- the Orchestrator-Worker system that decomposes tasks, coordinates workers, and interacts with the Rust engine via the Engine abstraction.

**Architecture**:
- **Orchestrator**: Task decomposition + worker coordination (LLM-powered)
- **Worker**: Subtask execution with LLM tool-calling + sandbox mode
- **Memory**: `ShortTermMemory` / `LongTermMemory` wrappers delegating to Engine
- **Search**: `SearchQuery` builder + `SearchResult` dataclasses
- **Engine**: Factory class that switches between local (PyO3) and remote (gRPC) mode

The Python layer does not access storage backends directly. All storage operations go through the Engine, which delegates to the Rust core.

---

## Guidelines Index

| Guide | Description | Status |
|-------|-------------|--------|
| [Directory Structure](./directory-structure.md) | Python package layout | Filled |
| [Component Guidelines](./component-guidelines.md) | Dataclass, Enum, Builder, Adapter patterns | Filled |
| [Hook Guidelines](./hook-guidelines.md) | Callback, event sourcing, checkpoint patterns | Filled |
| [State Management](./state-management.md) | Task state machine, in-memory state, computed properties | Filled |
| [Quality Guidelines](./quality-guidelines.md) | pytest patterns, mocking, test organization | Filled |
| [Type Safety](./type-safety.md) | Type annotations, Optional/List/Dict, Any for engine | Filled |

---

**Language**: All documentation is written in **English**.
