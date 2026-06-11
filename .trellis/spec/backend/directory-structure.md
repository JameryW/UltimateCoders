# Directory Structure

> How backend code is organized in this project.

---

## Overview

The backend is a Cargo workspace with 5 crates, each with a clear separation of concerns. Every sub-module in `uc-engine` follows a consistent `mod.rs` + implementation files pattern.

---

## Workspace Layout

```
Cargo.toml                  # Workspace root
crates/
  uc-types/src/             # Shared types + EngineApi trait
    engine.rs               # EngineApi trait, HealthStatus, ComponentHealth
    error.rs                # EngineError enum (13 variants)
    memory.rs               # MemoryKey, MemoryEntry, MemoryContent, MemoryMetadata
    search.rs               # SearchQuery, SearchResult, SearchMode
    index.rs                # IndexRequest, IndexResponse, IndexState, SymbolKind
    agent.rs                # TaskId, WorkerId, Task/Subtask status types
  uc-engine/src/            # Core engine (LocalEngine implements EngineApi)
    lib.rs                  # pub mod declarations + pub use re-exports
    local.rs                # LocalEngine struct + EngineApi impl
    config.rs               # EngineConfig, StorageConfig, MemoryConfig, EmbeddingConfig
    memory/                 # Layered memory subsystem
    indexer/                # Index pipeline (text, AST, semantic)
    search/                 # Hybrid search engine
    metadata/               # PostgreSQL structured metadata
    git/                    # Git repository management
    scheduler/              # Task scheduling
    sandbox/                # Sandbox execution (subprocess, Docker, agent adapters)
    conflict/               # Conflict detection + three-way merge
    events.rs               # Event sourcing types + EventStore trait
    checkpoint.rs           # Checkpoint manager with snapshot optimization
    rate_limiter.rs         # Token bucket + LLM rate limiter
    circuit_breaker.rs      # Circuit breaker pattern
  uc-grpc/src/              # gRPC server/client + proto-to-types conversions
    server.rs               # GrpcServer wrapping EngineApi
    client.rs               # GrpcEngineClient implementing EngineApi
    conversions.rs          # Proto <-> uc-types conversion functions
  uc-grpc-server/src/       # Standalone gRPC server binary
    main.rs                 # Server startup + graceful shutdown
  uc-python/src/            # PyO3 Python binding
    engine.rs               # PyEngine class + engine_error_to_pyerr mapping
    types.rs                # PyO3 type wrappers (PySearchQuery, PyMemoryEntry, etc.)
    async_support.rs        # pyo3-async-runtimes helpers
proto/                      # Shared proto definitions
```

---

## uc-engine Sub-module Pattern

Every sub-module follows this structure:

```
module_name/
  mod.rs          # Public API: struct definitions, impl blocks, pub use
  impl_file.rs    # Implementation details (e.g., text.rs, ast.rs, semantic.rs)
```

**Real examples**:

| Sub-module | Files | Purpose |
|---|---|---|
| `memory/` | `mod.rs`, `short_term.rs`, `long_term.rs` | Layered memory (TiKV short-term, Qdrant long-term) |
| `indexer/` | `mod.rs`, `text.rs`, `ast.rs`, `semantic.rs` | Index pipeline (text tokenizer, tree-sitter AST, BLAKE3/Voyage embeddings) |
| `search/` | `mod.rs`, `hybrid.rs`, `semantic.rs` | Hybrid search engine (text + AST + semantic) |
| `sandbox/` | `mod.rs`, `subprocess.rs`, `docker.rs`, `pool.rs`, `file_tracker.rs`, `agents/` | Sandbox execution |
| `sandbox/agents/` | `mod.rs`, `claude_code.rs`, `codex.rs` | Coding agent adapters |
| `metadata/` | `mod.rs`, `postgres.rs` | PostgreSQL structured metadata |
| `conflict/` | `mod.rs`, `merger.rs` | Conflict detection + three-way merge |

---

## lib.rs Re-export Pattern

`crates/uc-engine/src/lib.rs` declares modules and re-exports key types:

```rust
// Module declarations
pub mod local;
pub mod indexer;
pub mod memory;
// ...

// Re-exports for ergonomic access
pub use local::LocalEngine;
pub use config::{EngineConfig, StorageConfig, MemoryConfig, EmbeddingConfig};
pub use indexer::IndexPipeline;
pub use search::HybridSearchEngine;
// ...
```

Feature-gated re-exports use `#[cfg(feature = "...")]`:

```rust
#[cfg(feature = "docker")]
pub use sandbox::docker::DockerSandbox;
```

---

## Naming Conventions

| Item | Convention | Example |
|------|-----------|---------|
| Crate name | `uc-<domain>` (hyphenated) | `uc-types`, `uc-engine`, `uc-grpc` |
| Module directory | `snake_case` | `memory/`, `short_term.rs` |
| Struct | `PascalCase` | `ShortTermMemory`, `LocalEngine`, `HybridSearchEngine` |
| Enum | `PascalCase` | `EngineError`, `AgentEventType`, `CircuitState` |
| Enum variant | `PascalCase` | `SearchError`, `TaskCreated`, `HalfOpen` |
| Function / method | `snake_case` | `encode_key`, `is_connected`, `new_fallback` |
| Constant | `SCREAMING_SNAKE` | `VECTOR_SIZE` (in long_term.rs) |
| Feature flag | `snake_case` | `storage`, `indexing`, `docker` |
| Config struct | `PascalCase` + `Config` suffix | `EngineConfig`, `StorageConfig`, `CheckpointConfig` |
| Helper factory (test) | `make_` prefix | `make_entry()`, `make_store()` |

---

## Adding a New Sub-module

1. Create `crates/uc-engine/src/new_module/mod.rs` with the public API
2. Add implementation files as needed (e.g., `new_module/impl.rs`)
3. Declare `pub mod new_module;` in `lib.rs`
4. Add `pub use` re-exports for key types in `lib.rs`
5. If the module needs a storage backend, follow the fallback pattern (see database-guidelines.md)
6. Add `#[cfg(test)] mod tests` at the bottom of each file
