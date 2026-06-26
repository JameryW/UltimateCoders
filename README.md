# UltimateCoders

[![Rust CI](https://github.com/JameryW/UltimateCoders/actions/workflows/ci-rust.yml/badge.svg)](https://github.com/JameryW/UltimateCoders/actions/workflows/ci-rust.yml)
[![Python CI](https://github.com/JameryW/UltimateCoders/actions/workflows/ci-python.yml/badge.svg)](https://github.com/JameryW/UltimateCoders/actions/workflows/ci-python.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Distributed AI Coding System with shared layered memory and multi-repo hybrid retrieval (Text + Semantic + AST).

Multiple AI coding agents collaborate on software tasks using an Orchestrator-Worker pattern. The Rust core handles indexing, search, memory, and scheduling. The Python agent layer handles LLM interaction for task decomposition and code generation. They communicate via PyO3 FFI (local) or gRPC (distributed), switchable at runtime. A broadcast channel delivers real-time task events to Dashboard consumers. The UC Orchestrator runs as an oh-my-pi (OMP) extension, providing rich terminal interaction with subtask progress widgets, overlays, and custom message rendering.

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

### 6. Run UC Orchestrator

```bash
# Start OMP with UC Orchestrator extension (recommended)
./run-omp.sh

# Or with gRPC server for distributed scenarios
./scripts/run_tui.sh --server
```

The UC Orchestrator runs inside OMP's terminal UI. Use `/uc submit <description>` to submit tasks, `/uc status` to check progress, and `/uc cancel/pause/resume` for control. Keyboard shortcuts: **Ctrl+T** for subtask tree overlay, **Ctrl+Shift+T** for task list.

The Dashboard (Vite + React) provides a web UI at `http://localhost:5173` for cluster monitoring.

## Architecture

See [docs/architecture.md](docs/architecture.md) for the full architecture document.

```
+-------------------+     +-------------------+     +---------------+     +---------------+
|   Python Agent    |     |   Python Agent    |     |  OMP + UC     |     |  Dashboard    |
|   Orchestrator    |     |     Worker        |     |  Extension    |     |  (Vite/React) |
+--------+----------+     +--------+----------+     +-------+-------+     +-------+-------+
         |                         |                         |                     |
         |  Engine API (PyO3)      |                         | gRPC                | SSE
         |                         |                         |                     |
+--------v-------------------------v----------+     +--------v-------+             |
|              Rust Core Engine               |     |  uc-grpc-server|             |
|  +----------+ +--------+ +---------+       |     +--------+-------+             |
|  | Indexer  | | Search | | Memory  |       |            |                       |
|  +----------+ +--------+ +---------+       |            | broadcast channel     |
|  +----------+ +----------+ +---------+     |            | (TaskEvent)            |
|  |Scheduler | |Checkpoint| |Conflict|      |            |                       |
|  +----------+ +----------+ +---------+     |            |                       |
+--------+----------+---------+---------+-----+            |                       |
         |          |         |         |                  |                       |
    +----v---+ +----v---+ +--v----+ +--v----+              |                       |
    |  TiKV  | | Qdrant | | PgSQL | | NATS  |<-------------+-----------------------+
    +--------+ +--------+ +-------+ +-------+    NATS pub/sub (task events)
```

### Real-Time Event Flow

All task events flow through a unified **broadcast channel** (capacity 256) in the gRPC server:

1. **Local decomposition** — TaskStore records events and broadcasts them
2. **LocalWorkerBridge** — Python subprocess sends JSON-RPC notifications; the bridge applies updates and broadcasts
3. **NATS subscriber** — Receives `uc.task.update` and `uc.task.event` from the Python NATS Worker; applies and broadcasts
4. **WatchTask stream** — Subscribes to the broadcast channel for instant delivery (replaces polling)

### LocalWorkerBridge

When NATS is unavailable, the gRPC server can execute tasks locally via a Python subprocess (`python -m ultimate_coders.local_worker`). Communication uses JSON-RPC 2.0 over stdin/stdout. The bridge:

- Spawns and manages the worker lifecycle
- Sends `submit_task` requests and reads progress notifications
- Applies worker updates to TaskStore and broadcasts events
- Falls back to newline-split decomposition if the worker is unavailable

### NATS Worker

An independent process that bridges the gRPC TaskService with the Python Orchestrator:

1. Subscribes to `uc.task.submit` (from gRPC server)
2. Calls `Orchestrator.submit_task()` for LLM/sandbox decomposition
3. Publishes status updates to `uc.task.update`
4. Publishes real-time events to `uc.task.event`
5. Sends heartbeats to `uc.heartbeat` every 30 seconds

### Multi-Worker Distributed Architecture

Multiple NATS Worker processes can collaborate on a single task:

- **NATS queue group** — each subtask delivered to exactly one worker via `uc.subtask.execute`
- **Worker discovery** — default-mode NatsWorker monitors `uc.heartbeat` for remote workers
- **Conditional dispatch** — remote workers available → dispatch to NATS; no remote workers → local execution (zero-config compat)
- **File conflict detection** — `ConflictDetector` blocks subtasks with overlapping file constraints
- **Worker failover** — stale worker detection (>90s no heartbeat) → subtask reassignment with retry limit (max 3)
- **Event-driven scheduling** — `asyncio.Event` wakes dispatch loop immediately on subtask completion/failure

### Repository Structure

```
ultimate-coders/
├── Cargo.toml                # Workspace root
├── pyproject.toml            # Maturin build config
├── docker-compose.yml        # Development storage backends
├── crates/
│   ├── uc-types/             # Shared types + EngineApi trait
│   ├── uc-engine/            # Core engine (LocalEngine implementation)
│   ├── uc-grpc/              # gRPC server/client + proto + broadcast + LocalWorkerBridge
│   ├── uc-grpc-server/       # Standalone gRPC server binary
│   └── uc-python/            # PyO3 Python binding
├── python/
│   └── ultimate_coders/      # Python ergonomic layer
│       ├── engine.py         # create_engine() factory
│       ├── agent/            # Orchestrator + Worker + Sandbox + Scheduler
│       ├── dashboard/        # Vite/React web dashboard + gRPC-Web streaming
│       ├── local_worker.py   # JSON-RPC worker subprocess
│       ├── nats_worker.py    # NATS consumer/producer bridge
│       ├── search/           # SearchQuery builder
│       ├── memory/           # Memory read/write interface
│       └── config.py         # Configuration loading
├── packages/
│   └── uc-orchestrator/        # OMP extension — task orchestration + rich TUI
│       ├── src/
│       │   ├── extension.ts    # Extension entry point
│       │   ├── orchestrator/   # Core orchestration logic
│       │   └── ui/             # pi-tui components (progress widget, overlays, renderers)
│       └── uc-rpc-server.ts    # JSONL stdio bridge for Python
├── scripts/
│   └── run_tui.sh              # Start OMP + optional gRPC server + dashboard
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

### UC Orchestrator

```bash
# Start OMP with UC extension
./run-omp.sh

# With gRPC server
./scripts/run_tui.sh --server
```

### Docker Compose

```bash
# Start all storage backends
docker compose up -d

# Stop everything
docker compose down

# Stop and remove volumes
docker compose down -v
```

## CI

Two independent CI workflows run on PRs targeting `main`:

| Workflow | Trigger paths | Checks |
|----------|--------------|--------|
| **Rust CI** | `crates/`, `Cargo.toml`, `Cargo.lock` | check, clippy, fmt, test (3 feature combos) |
| **Python CI** | `python/`, `tests/`, `pyproject.toml` | ruff lint, pytest (3.9 + 3.12) |

Storage integration tests only run on `main` pushes or manual dispatch (requires Docker Compose infra).

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
| `UC_SANDBOX_MODE` | - | Sandbox mode: `subprocess` or empty |
| `UC_PROJECT_PATH` | - | Project path for sandbox execution |
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
- ✅ PR21-30: Broadcast channel + LocalWorkerBridge + Dashboard SSE + NATS Worker + TUI CI
- ✅ PR31: Replace TUI with OMP — rich progress widgets, subtask tree overlay, task list overlay, custom message renderer, JSONL event channel

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

# UC Orchestrator tests
cd packages/uc-orchestrator && npx tsc --noEmit
```

### Linting

```bash
# Rust
cargo clippy --workspace -- -D warnings
cargo fmt --all -- --check

# Python
ruff check python/ tests/

# UC Orchestrator
cd packages/uc-orchestrator && npx tsc --noEmit
```

## License

MIT
