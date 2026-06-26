# UltimateCoders

[![Rust CI](https://github.com/JameryW/UltimateCoders/actions/workflows/ci-rust.yml/badge.svg)](https://github.com/JameryW/UltimateCoders/actions/workflows/ci-rust.yml)
[![Python CI](https://github.com/JameryW/UltimateCoders/actions/workflows/ci-python.yml/badge.svg)](https://github.com/JameryW/UltimateCoders/actions/workflows/ci-python.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Distributed AI Coding System with shared layered memory and multi-repo hybrid retrieval (Text + Semantic + AST).

The UC Orchestrator runs as an oh-my-pi (OMP) extension, providing rich terminal interaction with subtask progress widgets, overlays, custom message rendering, and LLM-callable memory tools. It uses OMP's `runSubprocess` API to invoke Claude Code CLI for task decomposition and subtask execution. The Rust core handles indexing, search, memory, and scheduling. Python Worker/Sandbox handles the gRPC LocalWorkerBridge fallback path. A broadcast channel delivers real-time task events to Dashboard consumers.

## Quick Start

### Prerequisites

- Rust 1.75+ (stable)
- Python 3.9+
- Bun (for OMP runtime)
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

The OMP extension also registers LLM-callable memory tools (`uc_memory_read`, `uc_memory_write`, `uc_memory_search`) so the coding agent can access shared layered memory during task execution.

The Dashboard (Vite + React) provides a web UI at `http://localhost:5173` for cluster monitoring.

## Architecture

See [docs/architecture.md](docs/architecture.md) for the full architecture document.

```
+-------------------+     +-------------------------------+     +---------------+
|   Python Worker   |     |  OMP + UC Extension           |     |  Dashboard    |
|  (LocalWorker/    |     |  +-------------------------+  |     |  (Vite/React) |
|   NATS fallback)  |     |  │ Orchestrator Core       │  |     +-------+-------+
+--------+----------+     |  │  ├─ Scheduler (DAG)     │  |             |
         |                |  │  ├─ TaskStore (SQLite)  │  |     +-------v-------+
         | Engine API     |  │  ├─ GrpcBridge          │──┼────►│  uc-grpc-server|
         | (PyO3/gRPC)    |  │  ├─ ControlSignals      │  |     +-------+-------+
+--------v-----------+    |  │  └─ MemoryBridge (LLM) │  |             | broadcast
|  Rust Core Engine  |    |  +-------------------------+  |             | channel
|  +----------+      |    |  │ Coding Agent (OMP API)  │  |             | (TaskEvent)
|  | Indexer  |      |    |  │  └─ runSubprocess       │  |             |
|  +----------+      |    |  │     → claude -p ...     │  |             |
|  +--------+ +----+ |    |  +-------------------------+  |             |
|  | Search | |Mem | |    |  │ UI Components           │  |             |
|  +--------+ +----+ |    |  │  ├─ ProgressWidget      │  |             |
|  +----------+------| |    |  │  ├─ SubtaskTreeOverlay  │  |             |
|  |Scheduler|Ckpt  | |    |  │  ├─ TaskListOverlay     │  |             |
|  +----------+------| |    |  │  ├─ TaskResultRenderer  │  |             |
+---+------+---+--+--+ +    |  │  └─ FooterStatus        │  |             |
    |      |   |  |         +---------------+---------------+             |
 +--v--+ +v-+-v--v--+                        |                             |
 | TiKV | |Qdrant| PgSQL | NATS  <-----------+-----------------------------+
 +-----+ +------+-------+-------+   NATS pub/sub + gRPC WatchTask
```

### Real-Time Event Flow

All task events flow through a unified **broadcast channel** (capacity 256) in the gRPC server:

1. **Local decomposition** — TaskStore records events and broadcasts them
2. **LocalWorkerBridge** — Python subprocess sends JSON-RPC notifications; the bridge applies updates and broadcasts
3. **NATS subscriber** — Receives `uc.task.update` and `uc.task.event` from the Python NATS Worker; applies and broadcasts
4. **WatchTask stream** — Subscribes to the broadcast channel for instant delivery (replaces polling)

### OMP Extension Internals

The UC Orchestrator extension (`packages/uc-orchestrator`) is the primary user interface. Key components:

| Component | File | Role |
|-----------|------|------|
| **Extension entry** | `extension.ts` | Registers `/uc` command, shortcuts, message renderer, wires events → UI |
| **Orchestrator** | `orchestrator.ts` | Task lifecycle: submit → decompose → DAG waves → review → complete |
| **Scheduler** | `scheduler.ts` | DAG builder, file-overlap wave splitter, CircuitBreaker |
| **GrpcBridge** | `grpc-bridge.ts` | gRPC client for TaskService (submit, watch, control signals) |
| **MemoryBridge** | `memory-bridge.ts` | LLM-callable tools: `uc_memory_read/write/search` |
| **TaskStore** | `task-store.ts` | SQLite-backed task persistence + restore on startup |
| **ControlSignals** | `control-signal-subscriber.ts` | gRPC stream for pause/resume/cancel from external sources |
| **Events** | `events.ts` | Typed event emitter decoupling orchestration ↔ UI |

Agent definition prompts (`agents/decomposer.md`, `supervisor.md`, `worker.md`) configure the LLM roles for task decomposition, subtask review, and code generation.

### LocalWorkerBridge

When NATS is unavailable, the gRPC server can execute tasks locally via a Python subprocess (`python -m ultimate_coders.local_worker`). Communication uses JSON-RPC 2.0 over stdin/stdout. The bridge:

- Spawns and manages the worker lifecycle
- Sends `submit_task` requests and reads progress notifications
- Applies worker updates to TaskStore and broadcasts events
- Falls back to newline-split decomposition if the worker is unavailable

### NATS Worker

An independent process that bridges the gRPC TaskService with Python Worker/Sandbox:

1. Subscribes to `uc.task.submit` (from gRPC server)
2. Calls `Worker.execute_subtask()` for sandbox decomposition
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
├── run-omp.sh                # Start OMP with UC extension (primary interface)
├── docker-compose.yml        # Development storage backends
├── crates/
│   ├── uc-types/             # Shared types + EngineApi trait
│   ├── uc-engine/            # Core engine (LocalEngine implementation)
│   ├── uc-grpc/              # gRPC server/client + proto + broadcast + LocalWorkerBridge
│   ├── uc-grpc-server/       # Standalone gRPC server binary
│   └── uc-python/            # PyO3 Python binding
├── packages/
│   └── uc-orchestrator/      # OMP extension — task orchestration + rich TUI
│       ├── src/
│       │   ├── extension.ts  # Extension entry (commands, shortcuts, renderers)
│       │   ├── orchestrator/ # Core orchestration logic
│       │   │   ├── orchestrator.ts   # Main orchestrator (submit, cancel, pause, resume, DAG waves)
│       │   │   ├── scheduler.ts      # DAG builder, wave splitter, circuit breaker
│       │   │   ├── grpc-bridge.ts    # gRPC client for TaskService
│       │   │   ├── memory-bridge.ts  # LLM-callable memory tools (read/write/search)
│       │   │   ├── task-store.ts     # SQLite-backed task persistence
│       │   │   ├── control-signal-subscriber.ts  # gRPC stream control signals
│       │   │   └── events.ts         # Typed event emitter (orchestration ↔ UI)
│       │   ├── ui/           # pi-tui components
│       │   │   ├── progress-widget.ts       # Live subtask progress
│       │   │   ├── subtask-tree-overlay.ts  # Ctrl+T overlay
│       │   │   ├── task-list-overlay.ts     # Ctrl+Shift+T overlay
│       │   │   ├── task-result-renderer.ts  # Custom message renderer
│       │   │   ├── status-renderer.ts       # Footer connection status
│       │   │   └── status-formatter.ts      # Task list/detail formatting
│       │   ├── agents/       # Agent definition prompts (decomposer, supervisor, worker)
│       │   └── uc-rpc-server.ts  # JSONL stdio bridge for Python
│       └── uc-rpc-server.test.ts
├── python/
│   └── ultimate_coders/      # Python ergonomic layer
│       ├── engine.py         # create_engine() factory
│       ├── agent/            # Worker + Sandbox + Scheduler
│       ├── dashboard/        # FastAPI metrics + SSE streaming
│       ├── local_worker.py   # JSON-RPC worker subprocess (gRPC bridge)
│       ├── nats_worker.py    # NATS consumer/producer bridge
│       ├── search/           # SearchQuery builder
│       ├── memory/           # Memory read/write interface
│       └── config.py         # Configuration loading
├── dashboard/                # Vite + React web dashboard
├── docker/                   # Docker configs + Dockerfiles + scheduler config
├── scripts/
│   └── run_tui.sh            # Start OMP + optional gRPC server
├── tests/
│   ├── rust/                 # Rust integration tests
│   └── python/               # Python unit tests
└── vendor/                   # oh-my-pi (OMP runtime)
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
