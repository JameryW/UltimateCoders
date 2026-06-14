# Backend Development Guidelines

> Conventions for the Rust core engine and its Python/Go bridging layers.

---

## Overview

The backend is a **Rust + Python hybrid** architecture:

- **Rust core** (5 crates): `uc-types`, `uc-engine`, `uc-grpc`, `uc-grpc-server`, `uc-python`
- **Python agent layer**: Orchestrator + Worker with LLM tool-calling
- **Bridging**: PyO3 FFI (local) + gRPC (distributed), runtime switchable

The `EngineApi` trait (`crates/uc-types/src/engine.rs:25`) is the unified contract. Both `LocalEngine` (uc-engine) and `GrpcEngineClient` (uc-grpc) implement it. The Python `Engine` class delegates to whichever is active.

---

## Guidelines Index

| Guide | Description | Status |
|-------|-------------|--------|
| [Directory Structure](./directory-structure.md) | Crate layout, sub-module organization | Filled |
| [Database Guidelines](./database-guidelines.md) | Storage fallback pattern, key encoding, migrations | Filled |
| [Error Handling](./error-handling.md) | EngineError enum, triple mapping, wrapping patterns | Filled |
| [Quality Guidelines](./quality-guidelines.md) | Rust/Python test patterns, naming conventions | Filled |
| [Logging Guidelines](./logging-guidelines.md) | tracing crate, Python logging, log levels | Filled |
| [Scheduler Spec](./scheduler-spec.md) | Task scheduling, night-window guard, persistence contracts | Filled |
| [Dashboard Spec](./dashboard-spec.md) | FastAPI + SSE monitoring, API endpoints, fallback contracts | Filled |
| [Codegraph Integration](./codegraph-integration.md) | Worker codegraph client, tool registration, pre-processing, graceful degradation | Filled |

---

**Language**: All documentation is written in **English**.
