# UltimateCoders

[English](README.md) | [简体中文](README.zh-CN.md)

[![Rust CI](https://github.com/JameryW/UltimateCoders/actions/workflows/ci-rust.yml/badge.svg)](https://github.com/JameryW/UltimateCoders/actions/workflows/ci-rust.yml)
[![Python CI](https://github.com/JameryW/UltimateCoders/actions/workflows/ci-python.yml/badge.svg)](https://github.com/JameryW/UltimateCoders/actions/workflows/ci-python.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Distributed AI coding system with shared layered memory and multi-repository hybrid retrieval across text, semantic, and AST indexes.

The UC Orchestrator runs as an oh-my-pi (OMP) extension with terminal-native task interaction, subtask progress widgets, overlays, custom message rendering, and LLM-callable memory tools. The Python Worker/Sandbox defaults to the [xAI Grok Build](https://github.com/xai-org/grok-build) terminal coding agent (`grok`) for subtask execution; Claude Code and Codex remain supported adapters. OMP's local `runSubprocess` path remains separate for its own decomposition and execution flow. The Rust core handles indexing, search, memory, and scheduling, while a broadcast channel delivers live task events to TUI and API consumers.

## Key Features

- **DAG orchestration**: decompose natural-language tasks into observable subtasks, schedule dependency waves, and stream submitted, running, completed, and failed states.
- **Product home, operations dashboard, and TUI**: `/` explains capabilities and the execution path; the existing operations dashboard remains at `/dashboard` (and `#/dashboard`); `#/tui` connects to a real OMP session over WebSocket.
- **Shared TUI / OMP command layer**: `run/status/tasks/workers/search/logs` use the same UC semantics and stream results through TaskEvent.
- **Distributed workers**: workers register through `WorkerService`, publish heartbeats and capabilities, and receive capability- and load-aware dispatch from the Gateway; NATS carries cross-process subtasks.
- **Rust core**: Engine, Task, Dashboard, and Worker services expose unified gRPC/gRPC-Web interfaces with task recovery, event broadcast, and in-memory fallback.
- **Cross-repository hybrid retrieval**: one query can combine text, semantic, and AST retrieval across indexed Git repositories.
- **Layered memory**: short-term memory, long-term semantic memory, and structured metadata use TiKV, Qdrant, and PostgreSQL, with an in-memory fallback when dependencies are unavailable.
- **Flexible deployment**: run local OMP, a Docker Gateway, Docker Compose, or a multi-worker cluster; workers can use Grok Build, Claude Code, or Codex.

## Product Highlights

UltimateCoders turns terminal-based AI coding into an observable, schedulable execution platform:

| Capability | What it shows | User benefit |
| --- | --- | --- |
| Product dashboard home | Runtime Surface, OMP ↔ UC Loop, Command Deck, and use cases | Understand the product and enter a real execution path from one place |
| Product map | Command, Control, Execution, Knowledge, and Event layers | See how entry points, orchestration, workers, context, and results connect |
| Native OMP interaction | WebSocket PTY, live terminal output, and session takeover | Keep the terminal workflow while adding a browser control surface |
| DAG orchestration | `run/submit` creates subtasks and schedules dependency waves | Break down, track, and recover complex work |
| Unified control plane | TUI, OMP commands, and gRPC TaskService share command semantics | Keep task state consistent across entry points |
| Distributed workers | Registration, capabilities, heartbeats, and load-aware scheduling | Scale execution capacity around model and tool capabilities |
| Search and memory | Text + Semantic + AST retrieval with TiKV/Qdrant/PostgreSQL memory | Give coding agents reusable context across repositories |
| Reliable deployment | Rust Gateway, NATS, Docker, in-memory fallback, and task events | Move from a local workflow to a worker cluster without changing the product surface |

## Quick Start

### 1. Install prerequisites

- Rust 1.75+ (stable)
- Python 3.9+
- Bun (OMP runtime)
- [Grok Build CLI](https://docs.x.ai/build/overview) (default worker executor)
- Docker Compose (optional, for TiKV, Qdrant, PostgreSQL, and NATS)

Install Grok Build and provide an xAI API key for the default worker:

```bash
curl -fsSL https://x.ai/cli/install.sh | bash
export XAI_API_KEY=your-key
```

### 2. Start the local workflow

```bash
git clone https://github.com/JameryW/UltimateCoders.git
cd UltimateCoders
./run-omp.sh --build
```

`--build` builds the Python package first. The startup script launches the gRPC Gateway, FastAPI dashboard, Vite product UI, and OMP by default. Use `./run-omp.sh --no-server` when you only need OMP.

The three entry points are:

| Entry point | Use |
| --- | --- |
| `http://localhost:5173/` | Product home: capabilities, execution path, and Command Deck |
| `http://localhost:5173/dashboard` or `http://localhost:5173/#/dashboard` | Existing operations dashboard: tasks, workers, events, scheduler, search, files, and metrics |
| `http://localhost:5173/#/tui` | Real OMP PTY terminal: run shared UC commands and view live output |

Common commands:

```text
/uc submit <description>    Submit a task
/uc status                  Show task status
/uc pause <task-id>         Pause a task
/uc resume <task-id>        Resume a task
/uc cancel <task-id>        Cancel a task
```

### 3. Start other modes

```bash
# Distributed cluster: NATS + gRPC + multiple workers + OMP
./run-cluster.sh --workers 2

# Standalone Gateway: in-memory fallback or external storage
./run-gateway.sh up

# Gateway + local storage containers
./run-gateway.sh up --docker
```

For build, test, configuration, and external Git deployment details, see [Building](#building), [Configuration](#configuration), and [Distributed Worker + External Git Deployment](#distributed-worker--external-git-deployment).

## Technical Architecture

![UltimateCoders technical architecture](docs/screenshots/execution-architecture.png)

The diagram follows one task through the system: OMP/TUI handles interaction, the Rust Gateway owns TaskService, DAG scheduling, and worker registration, the Worker Pool executes subtasks, Search + Memory provide repository context, and Task Events return state to TUI, Dashboard, and API consumers.

| Layer | Components | Responsibility |
| --- | --- | --- |
| Interaction | OMP / TUI | Accept natural-language tasks through PTY, WebSocket, and gRPC-Web |
| Control plane | Rust Gateway | Task persistence, DAG scheduling, TaskService, EngineService, and WorkerService |
| Execution | Worker Pool | Dispatch by capability, heartbeat, and load to Grok Build, Claude Code, or Codex |
| Knowledge | Search + Memory | Combine Text, Semantic, and AST retrieval with TiKV, Qdrant, and PostgreSQL memory |
| Observability | Task Events | Broadcast submitted, running, completed, and failed states to TUI, Dashboard, and API |

## Product Preview

The product home (`/`) explains capabilities, the execution path, OMP interaction, and Command Deck. The existing operations dashboard (`#/dashboard`) remains the detailed monitoring surface, while `#/tui` connects to the real OMP PTY and Gateway TaskService.

### Dashboard home and live entry points

The product home is the navigation layer between product understanding and real execution:

- **Runtime Surface**: show Gateway status, version, task count, and WatchTask state through the existing gRPC-Web connection.
- **OMP ↔ UC Loop**: trace `OMP terminal → UC Extension → Rust Gateway → Worker → TaskEvent` and switch the interaction trace by stage.
- **Command Deck**: inspect service paths, example input, and typical output for `run`, `status`, `tasks`, `workers`, `search`, and `logs`.
- **Product Map**: explain the responsibilities, protocols, and benefits of the Command, Control, Execution, Knowledge, and Event planes.
- **Live handoff**: enter `#/tui` from the Command Deck or OMP flow; execution still uses the real WebSocket + gRPC-Web path.
- **Single-session takeover**: the OMP PTY keeps one active browser session; another tab can use `Take over` to move the persistent session without restarting OMP.

Local preview: `http://127.0.0.1:4176/`; live terminal: `http://127.0.0.1:4176/#/tui`.

### Product capabilities

![UltimateCoders product capabilities](docs/screenshots/product-capabilities.png)

This overview covers DAG orchestration, capability-aware workers, Hybrid Search + Memory, native OMP interaction, event-driven recovery, and the path from a local workflow to a cluster.

### Product use cases

![UltimateCoders product use cases](docs/screenshots/product-scenarios.png)

UltimateCoders targets large-repository changes, parallel delivery, incident diagnosis, and local-to-cluster expansion. The recurring benefits are reusable context, observable execution, and scalable capacity.

### TUI / OMP terminal

`#/tui` is the OMP interaction entry point. The header shows session state, the command bar runs shared UC commands, and command results plus Gateway responses remain in the terminal. In Docker/WSL or environments with PTY/WebSocket support, the terminal also shows live OMP output.

#### Full product walkthrough

![UltimateCoders TUI full product walkthrough](docs/screenshots/tui-terminal.png)

This view brings together the OMP connection, Gateway state, worker capabilities, search entry points, event log, real task submission, and TaskService queries.

#### Command and capability catalog

![UltimateCoders TUI command catalog](docs/screenshots/tui-command-catalog.png)

The shared command bar exposes `status`, `tasks`, `workers`, `search`, `logs`, `submit/run`, and pause/resume/cancel actions. The product home explains the service path behind each command.

#### DAG task submission

![UltimateCoders TUI DAG task submission](docs/screenshots/tui-task-dag.png)

Natural-language tasks go through the real TaskService, return a task ID, and enter the DAG scheduling path.

### Product demo video

<video controls preload="metadata" width="100%" poster="https://raw.githubusercontent.com/JameryW/UltimateCoders/main/docs/screenshots/execution-architecture.png">
  <source src="https://raw.githubusercontent.com/JameryW/UltimateCoders/main/docs/videos/ultimatecoders-product-showcase.mp4" type="video/mp4">
  Your browser does not support inline video. [Open the product demo video](https://raw.githubusercontent.com/JameryW/UltimateCoders/main/docs/videos/ultimatecoders-product-showcase.mp4).
</video>

[Download the full product demo](docs/videos/ultimatecoders-product-showcase.mp4) · [Open the video file directly](https://raw.githubusercontent.com/JameryW/UltimateCoders/main/docs/videos/ultimatecoders-product-showcase.mp4)

The video starts with the product overview, use cases, and execution architecture, then walks through a real page session: OMP WebSocket connection, Gateway status, worker/search/log queries, a `run` task in TUI, a real task ID entering the DAG, and a `tasks` query against TaskService.

See the [TUI interaction detail video](docs/videos/ultimatecoders-tui-demo.mp4) for command-bar, terminal-output, and OMP WebSocket details.

## Runtime and architecture

The product dashboard (Vite + React) is available at `http://localhost:5173/` with the default Vite config. Its root route is the product overview; open `#/tui` for the real OMP PTY terminal connected to the Gateway TaskService over gRPC-Web. The existing operations dashboard remains at `#/dashboard`.

See [docs/architecture.md](docs/architecture.md) for the detailed architecture reference. The runtime can be read in five layers:

| Layer | Responsibility | Main interfaces |
| --- | --- | --- |
| Command | OMP, TUI and Dashboard entry points | `/uc`, Command Deck, gRPC-Web |
| Control | Task lifecycle, DAG scheduling and persistence | TaskService, TaskStore, control signals |
| Execution | Local fallback and capability-aware Workers | WorkerService, NATS, sandbox |
| Knowledge | Repository indexing, hybrid search and layered memory | Text, Semantic, AST, TiKV, Qdrant, PostgreSQL |
| Events | Live progress, recovery and monitoring updates | TaskEvent, broadcast channel, SSE, WatchTask |


### Real-Time Event Flow

All task events flow through a unified **broadcast channel** (capacity 256) in the gRPC server:

1. **Local decomposition** — TaskStore records events and broadcasts them
2. **Local fallback** — in-process newline-split decomposition records events and broadcasts them (no external worker)
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
| **MemoryBridge** | `memory-bridge.ts` | LLM-callable tool: `uc_memory` (read/write/search/delete) |
| **TaskBridge** | `task-bridge.ts` | LLM-callable tool: `uc_task` (submit/cancel/pause/resume/status) |
| **IndexBridge** | `index-bridge.ts` | LLM-callable tool: `uc_index` (index_repo/list_repos/get_state/remove_index) |
| **FileBridge** | `file-bridge.ts` | LLM-callable tool: `uc_file` (list_dir/get_file) |
| **WorkerBridge** | `worker-bridge.ts` | LLM-callable tool: `uc_worker` (list/status/scale/deregister) |
| **TaskStore** | `task-store.ts` | SQLite-backed task persistence + restore on startup |
| **ControlSignals** | `control-signal-subscriber.ts` | gRPC stream for pause/resume/cancel from external sources |
| **Events** | `events.ts` | Typed event emitter decoupling orchestration ↔ UI |

Agent definition prompts (`agents/decomposer.md`, `supervisor.md`, `worker.md`) configure the LLM roles for task decomposition, subtask review, and code generation.

### Local Fallback (No NATS)

When NATS is unavailable, the gRPC server executes tasks locally via in-process newline-split decomposition (the legacy `python -m ultimate_coders.local_worker` JSON-RPC subprocess path has been removed). The server:

- Decomposes the task description into subtasks by newline-split heuristic
- Applies updates to TaskStore and broadcasts events through the same channel
- Degrades gracefully — no external worker process required

### NATS Worker

An independent process that bridges the gRPC TaskService with Python Worker/Sandbox:

1. Subscribes to `uc.task.submit` (from gRPC server)
2. Calls `Worker.execute_subtask()` for sandbox decomposition
3. Publishes status updates to `uc.task.update`
4. Publishes real-time events to `uc.task.event`
5. Sends heartbeats to `uc.heartbeat` every 30 seconds

The worker invokes `grok -p ... --output-format streaming-json` by default. Set
`UC_CODING_AGENT=claude-code` or `UC_CODING_AGENT=codex` when an existing
deployment needs one of the compatibility adapters.

### Multi-Worker Distributed Architecture

Multiple NATS Worker processes can collaborate on a single task:

- **NATS queue group** — each subtask delivered to exactly one worker via `uc.subtask.execute`
- **Worker discovery** — default-mode NatsWorker monitors `uc.heartbeat` for remote workers
- **Conditional dispatch** — remote workers available → dispatch to NATS; no remote workers → local execution (zero-config compat)
- **File conflict detection** — `ConflictDetector` blocks subtasks with overlapping file constraints
- **Worker failover** — stale worker detection (>90s no heartbeat) → subtask reassignment with retry limit (max 3)
- **Event-driven scheduling** — `asyncio.Event` wakes dispatch loop immediately on subtask completion/failure

### Repository Structure

| Path | Purpose |
| --- | --- |
| `crates/` | Rust core, Engine API, gRPC services and PyO3 binding |
| `packages/uc-orchestrator/` | OMP extension, DAG orchestration, UC tools and terminal UI |
| `python/ultimate_coders/` | Python Engine facade, Worker/Sandbox, search, memory and FastAPI dashboard |
| `dashboard/` | Vite + React product homepage, legacy operations dashboard and TUI terminal |
| `docker/` | Gateway, Worker, storage and compose configuration |
| `tests/python/` | Python unit tests |
| `run-omp.sh`, `run-cluster.sh`, `run-gateway.sh` | Local, clustered and standalone startup entry points |

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
python -m pip install -e ".[test]"
pytest tests/python/ -v        # Run Python tests
```

### UC Orchestrator

```bash
# Start OMP with UC extension (gRPC server starts by default)
./run-omp.sh

# Skip gRPC server
./run-omp.sh --no-server

# Ensure Python package is built first
./run-omp.sh --build

# Standalone: gateway runs in a container (in-memory/external-storage fallback)
./run-omp.sh --standalone
# Standalone + local storage containers
./run-omp.sh --standalone --docker

# Start distributed cluster instead
./run-cluster.sh
# Standalone cluster: container gateway + storage + host workers
./run-cluster.sh --standalone --workers 2
```

### Standalone Gateway (containerized)

```bash
# Gateway container only — in-memory fallback, or external storage via env
./run-gateway.sh up
# Gateway + local storage containers (TiKV/Qdrant/PG/NATS)
./run-gateway.sh up --docker
# Status / logs / stop
./run-gateway.sh status
./run-gateway.sh logs
./run-gateway.sh down [--docker]

# External storage (default mode, no --docker): point at remote backends,
# empty = in-memory fallback.
#   UC_TIKV_PD_ENDPOINTS=pd.example:2379 UC_QDRANT_URL=http://qdrant.example:6334 \
#     UC_PG_URL=postgresql://u:p@pg.example:5432/uc UC_NATS_URL=nats://nats.example:4222 \
#     ./run-gateway.sh up
```

### Docker Compose (storage backends)

```bash
# Build and start the complete local app, including the React Dashboard UI.
docker compose -f docker/docker-compose.yml --profile app up --build
# React UI: http://localhost:8081
# Dashboard API: http://localhost:8080/dashboard/

# Start all storage backends
docker compose -f docker/docker-compose.yml up -d

# Stop everything
docker compose -f docker/docker-compose.yml down

# Stop and remove volumes
docker compose -f docker/docker-compose.yml down -v
```

### Distributed Worker + External Git Deployment

Workers can run containerized and sync code from an **external git remote**
(GitHub/GitLab), making the remote the unified source of truth across hosts.
This is **opt-in**: without `UC_REPO_URL` the legacy local-only workspace
mode is used.

**Configuration** (set on the `worker` / `nats-worker` services):

| Variable | Default | Description |
|----------|---------|-------------|
| `UC_REPO_URL` | _(empty)_ | External git remote URL. Empty = local-only workspace. |
| `UC_REPO_BASE_BRANCH` | `main` | Base branch workers branch off; the arbiter merges into it. |
| `UC_GIT_TOKEN` | _(empty)_ | PAT, injected via `GIT_ASKPASS` (never on the URL/args). |
| `UC_GIT_FETCH_ON_ACQUIRE` | `true` | `git fetch` before each worktree acquire. |
| `UC_GIT_PUSH_ON_RELEASE` | `false` | Push the `uc/subtask/<id>` branch on release. |
| `UC_GIT_MERGE_ARBITRATE` | _(env)_ | Orchestrator `MergeArbiter` merges subtask branches into `origin/main` and pushes. |

**Flow:**

1. Each worker clones `UC_REPO_URL` into a persistent volume on first start.
2. Each subtask runs in a git worktree branched off `origin/<base_branch>`.
3. On release, the worker pushes `uc/subtask/<id>` (workers never touch `main`).
4. The Orchestrator's `MergeArbiter` merges subtask branches into `origin/main`
   and pushes `main` (the only writer of `main`).

**Conflict model:** `DistributedConflictDetector` is an advisory in-process
scheduling hint, NOT a distributed lock. The authoritative cross-worker
conflict point is git merge-time (`MergeArbiter`).

**Cross-host scaling:** `docker compose --scale worker=N` scales workers on
the **same host** only (the gateway shells out to the local `docker.sock`).
True cross-host scaling requires docker swarm / a remote docker context /
per-host gateways (future work). The external-git design is already
cross-host-safe at the data level: each host runs its own compose and clones
from the same remote, and merge arbitration reconciles concurrent edits.

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
| `UC_PROJECT_PATH` | - | Project path for sandbox execution |
| `UC_CODING_AGENT` | `grok-build` | Worker coding agent (`grok-build`/`grok`, `claude-code`, or `codex`) |
| `XAI_API_KEY` | - | xAI API key for the default Grok Build worker agent |
| `ANTHROPIC_API_KEY` | - | Anthropic API key for Claude Code calls |
| `OPENAI_API_KEY` | - | OpenAI API key for Codex calls |

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

# Python tests and runtime dependencies
python -m pip install -e ".[test]"
pytest tests/python/ -v

# Python tests with Rust extension
python -m pip install -e ".[test]" && pytest tests/python/ -v

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
