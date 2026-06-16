# UltimateCoders

[![CI](https://github.com/JameryW/UltimateCoders/actions/workflows/ci.yml/badge.svg)](https://github.com/JameryW/UltimateCoders/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/JameryW/UltimateCoders)](https://github.com/JameryW/UltimateCoders/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Distributed AI Coding System with shared layered memory and multi-repo hybrid retrieval (Text + Semantic + AST).

Multiple AI coding agents collaborate on software tasks using an Orchestrator-Worker pattern. The Rust core handles indexing, search, memory, and scheduling. The Python agent layer handles LLM interaction for task decomposition and code generation. They communicate via PyO3 FFI (local) or gRPC (distributed), switchable at runtime. An Ink-based TUI provides real-time task monitoring with CJK/IME support.

## Quick Start

### Prerequisites

- Rust 1.75+ (stable)
- Python 3.9+
- Node.js 18+ (for TUI)
- Docker and Docker Compose (for storage backends)

### 1. Start Storage Backends

```bash
docker compose up -d
```

This starts TiKV, Qdrant, PostgreSQL, and NATS. See [Configuration](#configuration) for connection details.

### 2. Build the Rust Core

```bash
cargo check          # Verify compilation
cargo test           # Run all tests (with in-memory fallbacks)
```

### 3. Build the Python Package

```bash
pip install maturin
maturin develop      # Build Rust extension + install in editable mode
```

### 4. Use from Python

```python
from ultimate_coders.engine import create_engine

# Local mode (Rust runs in-process)
engine = create_engine(mode="local")

# Health check
status = engine.health()

# Index a repository
engine.index_repo("my-project", "/path/to/repo")

# Search across indexed repos
from ultimate_coders.search.query import SearchQuery
query = SearchQuery("database connection").in_repo("my-project").text_mode()
results = engine.search(query)

# Memory operations
engine.write_memory("task", "decisions", "Use PostgreSQL for metadata", task_id="t1")
entry = engine.read_memory("task", "decisions", task_id="t1")
```

### 5. Start the gRPC Server

```bash
cargo run -p uc-grpc-server
```

Then connect from Python:

```python
engine = create_engine(mode="grpc", grpc_endpoint="http://localhost:50051")
```

### 6. Start the TUI

```bash
cd tui
npm install
npm start
```

The TUI connects to the gRPC server and provides real-time task monitoring with CJK/IME input support. See [tui/README.md](tui/README.md) for keyboard shortcuts and architecture details.

## Architecture

See [docs/architecture.md](docs/architecture.md) for the full architecture document.

```
+-------------------+     +-------------------+     +---------------+
|   Python Agent    |     |   Python Agent    |     |  Ink TUI      |
|   Orchestrator    |     |     Worker        |     |  (Node.js)    |
+--------+----------+     +--------+----------+     +-------+-------+
         |                         |                         |
         |  Engine API (PyO3)      |                         | gRPC
         |                         |                         |
+--------v-------------------------v----------+     +--------v-------+
|              Rust Core Engine               |     |  uc-grpc-server|
|  +----------+ +--------+ +---------+       |     +--------+-------+
|  | Indexer  | | Search | | Memory  |       |              |
|  +----------+ +--------+ +---------+       |              |
|  +----------+ +----------+ +---------+     |              |
|  |Scheduler | |Checkpoint| |Conflict|      |              |
|  +----------+ +----------+ +---------+     |              |
+--------+----------+---------+---------+-----+              |
         |          |         |         |                     |
    +----v---+ +----v---+ +--v----+ +--v----+                |
    |  TiKV  | | Qdrant | | PgSQL | | NATS  |<---------------+
    +--------+ +--------+ +-------+ +-------+
```

### Repository Structure

```
ultimate-coders/
├── Cargo.toml                # Workspace root
├── pyproject.toml            # Maturin build config
├── docker-compose.yml        # Development storage backends
├── crates/
│   ├── uc-types/             # Shared types + EngineApi trait
│   ├── uc-engine/            # Core engine (LocalEngine implementation)
│   ├── uc-grpc/              # gRPC server/client + proto
│   ├── uc-grpc-server/       # Standalone gRPC server binary
│   └── uc-python/            # PyO3 Python binding
├── python/
│   └── ultimate_coders/      # Python ergonomic layer
│       ├── engine.py         # create_engine() factory
│       ├── agent/            # Orchestrator + Worker
│       ├── search/           # SearchQuery builder
│       ├── memory/           # Memory read/write interface
│       └── config.py         # Configuration loading
├── tui/                      # Ink-based Terminal UI
│   ├── src/
│   │   ├── components/       # React/Ink UI components
│   │   ├── hooks/            # gRPC connection + event hooks
│   │   ├── grpc/             # Node.js gRPC client
│   │   ├── reducer.ts        # Central state management
│   │   └── keymap.ts         # Keyboard command definitions
│   └── vitest.config.ts      # Test configuration
├── proto/                    # Protobuf definitions
├── tests/
│   ├── rust/                 # Rust integration tests
│   └── python/               # Python unit tests
└── docs/
    └── architecture.md       # Architecture document
```

## Building

### Rust

```bash
cargo check                    # Check all crates compile
cargo test                     # Run all tests (in-memory fallbacks)
cargo test --features storage  # Run tests with real storage backends
cargo test --features indexing # Run tests with AST indexing enabled
cargo clippy --workspace       # Lint
cargo fmt --all -- --check     # Format check
```

### Python

```bash
maturin develop                # Build and install in editable mode
pytest tests/python/ -v        # Run Python tests
```

### TUI

```bash
cd tui
npm install                    # Install dependencies
npm start                      # Start TUI
npm test                       # Run 280+ unit tests (vitest)
npm run typecheck              # TypeScript type checking
```

### Docker Compose

```bash
# Start all storage backends
docker compose up -d

# Start with development tools (pgAdmin, NATS box)
docker compose -f docker-compose.yml -f docker-compose.dev.yml --profile tools up -d

# Start with gRPC server
docker compose -f docker-compose.yml -f docker-compose.dev.yml --profile engine up -d

# Stop everything
docker compose down

# Stop and remove volumes
docker compose down -v
```

## Configuration

Configuration is loaded from environment variables with sensible defaults. No config file required for development.

| Variable | Default | Description |
|----------|---------|-------------|
| `UC_ENGINE_MODE` | `local` | Engine mode: `local` (PyO3 FFI) or `grpc` (remote) |
| `UC_GRPC_ADDR` | `[::]:50051` | gRPC server listen address |
| `UC_GRPC_ENDPOINT` | - | gRPC server endpoint (required for grpc mode) |
| `UC_TIKV_PD_ENDPOINTS` | `127.0.0.1:2379` | TiKV Placement Driver endpoints (comma-separated) |
| `UC_QDRANT_URL` | `http://127.0.0.1:6333` | Qdrant REST API URL |
| `UC_QDRANT_API_KEY` | - | Qdrant API key (optional) |
| `UC_POSTGRES_URL` | `postgresql://localhost:5432/ultimatecoders` | PostgreSQL connection URL |
| `UC_NATS_URL` | `nats://127.0.0.1:4222` | NATS server URL |
| `ANTHROPIC_API_KEY` | - | Anthropic API key for LLM calls |

Docker Compose default credentials:

| Service | Host | Port | User | Password |
|---------|------|------|------|----------|
| PostgreSQL | localhost | 5432 | `ultimate_coders` | `ultimate_coders` |
| Qdrant REST | localhost | 6333 | - | - |
| Qdrant gRPC | localhost | 6334 | - | - |
| TiKV PD | localhost | 2379 | - | - |
| NATS | localhost | 4222 | - | - |
| NATS Monitor | localhost | 8222 | - | - |

## Development Progress

- ✅ PR1: Rust workspace + uc-types + uc-engine skeleton
- ✅ PR2: 存储客户端集成 + Memory 读写 (in-memory fallback; TiKV/Qdrant/PostgreSQL clients coded, need infra)
- ✅ PR3: 文本检索 + AST 索引引擎 (language-aware tokenization, tree-sitter AST, text search)
- ✅ PR4: 语义检索 + 混合检索 API (BLAKE3 fallback embeddings, hybrid search engine)
- ✅ PR5: gRPC + PyO3 桥接层 (tonic server/client, proto compilation, PyEngine wired)
- ✅ PR6: Python Agent 层 (Orchestrator + Worker, LLM tool-calling, memory wrappers)
- ✅ PR7: 容错机制 (Event Sourcing, Checkpoint/Resume, Conflict Detection, Rate Limiting, Circuit Breaker)
- ✅ PR8: Docker Compose + CI + 文档 (TiKV/Qdrant/PostgreSQL/NATS, GitHub Actions, architecture docs)
- ✅ PR9: Sandbox Agent Executor (SubprocessSandbox + DockerSandbox, Claude Code + Codex adapters, Worker sandbox mode)
- ✅ PR10: 任务调度与夜间编排 (tokio-cron-scheduler, NightWindow Guard, ScheduleStore, Orchestrator 独占模式, YAML 配置)
- ✅ PR11-20: TUI 实时监控 (Ink + React, gRPC streaming, CJK/IME input, segment-based StatusBar, 280+ tests)

## Development

### Running Tests

```bash
# Rust unit tests (no storage required)
cargo test --no-default-features

# Rust tests with indexing feature
cargo test --features indexing

# Rust tests with real storage (requires Docker Compose)
cargo test --features storage

# Python tests (pure Python, no Rust extension needed)
PYTHONPATH=python pytest tests/python/ -v

# Python tests with Rust extension
maturin develop && pytest tests/python/ -v

# TUI tests
cd tui && npm test
```

### Linting

```bash
# Rust
cargo clippy --workspace -- -D warnings
cargo fmt --all -- --check

# Python
ruff check python/ tests/

# TUI
cd tui && npm run typecheck
```

## License

MIT
