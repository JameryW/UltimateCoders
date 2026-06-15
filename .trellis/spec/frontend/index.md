# Python Agent Layer Guidelines

> Conventions for the Python agent layer -- Orchestrator, Worker, Memory, and Search.

---

## Overview

The "frontend" in this project covers two layers:

1. **Python Agent Layer** — the Orchestrator-Worker system that decomposes tasks, coordinates workers, and interacts with the Rust engine via the Engine abstraction.
2. **TUI (Terminal UI)** — the Ink/React terminal interface that connects to the gRPC server for task submission and real-time event streaming.

**Architecture (Python Agent Layer)**:
- **Orchestrator**: Task decomposition + worker coordination (LLM-powered)
- **Worker**: Subtask execution with LLM tool-calling + sandbox mode
- **Memory**: `ShortTermMemory` / `LongTermMemory` wrappers delegating to Engine
- **Search**: `SearchQuery` builder + `SearchResult` dataclasses
- **Engine**: Factory class that switches between local (PyO3) and remote (gRPC) mode

The Python layer does not access storage backends directly. All storage operations go through the Engine, which delegates to the Rust core.

**Architecture (TUI)**:
- **Ink 5 + React 18** — Terminal rendering framework
- **gRPC Client** — `@grpc/grpc-js` + `@grpc/proto-loader` connecting to uc-grpc-server
- **Hooks** — `useGrpcClient` (connection management), `useTaskEvents` (stream subscription)
- **Components** — App, ChatLog, SubtaskTree, TaskInput (CJK-aware), CjkTextInput, StatusBar, LogoHeader

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
| [TUI gRPC Spec](./tui-grpc-spec.md) | TUI gRPC client, React hooks, streaming, offline fallback | Filled |

---

**Language**: All documentation is written in **English**.
