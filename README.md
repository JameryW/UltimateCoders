# UltimateCoders

[English](README.md) | [简体中文](README.zh-CN.md)

[![Rust CI](https://github.com/JameryW/UltimateCoders/actions/workflows/ci-rust.yml/badge.svg)](https://github.com/JameryW/UltimateCoders/actions/workflows/ci-rust.yml)
[![Python CI](https://github.com/JameryW/UltimateCoders/actions/workflows/ci-python.yml/badge.svg)](https://github.com/JameryW/UltimateCoders/actions/workflows/ci-python.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Distributed AI coding system with shared layered memory and multi-repository hybrid retrieval across text, semantic, and AST indexes.

The default Docker app uses the Web Dashboard, Rust Gateway, NATS, and Python Workers. The optional oh-my-pi (OMP) extension remains available through `run-omp.sh` for native terminal workflows. The Python Worker/Sandbox defaults to the [xAI Grok Build](https://github.com/xai-org/grok-build) coding agent (`grok`); Claude Code, Codex, and the local harness are supported adapters. The Rust core handles indexing, search, memory, and scheduling, while a broadcast channel delivers live task events to Dashboard and API consumers.

## Key Features

- **DAG orchestration**: decompose natural-language tasks into observable subtasks, schedule dependency waves, and stream submitted, running, completed, and failed states.
- **Product home and operations dashboard**: `/` explains capabilities and the execution path; `/dashboard` and `#/dashboard` provide live operations.
- **Optional OMP extension**: `run-omp.sh` provides native `/uc` commands and LLM-callable tools outside the default Docker app.
- **Distributed workers**: workers register through `WorkerService`, publish heartbeats and capabilities, and receive capability- and load-aware dispatch from the Gateway; NATS carries cross-process subtasks.
- **Rust core**: Engine, Task, Dashboard, and Worker services expose unified gRPC/gRPC-Web interfaces with task recovery, event broadcast, and in-memory fallback.
- **Cross-repository hybrid retrieval**: one query can combine text, semantic, and AST retrieval across indexed Git repositories.
- **Layered memory**: short-term memory, long-term semantic memory, and structured metadata use TiKV, Qdrant, and PostgreSQL, with an in-memory fallback when dependencies are unavailable.
- **Flexible deployment**: run local OMP, a Docker Gateway, Docker Compose, or a multi-worker cluster; workers can use Grok Build, Claude Code, or Codex.

## Product Highlights

UltimateCoders turns terminal-based AI coding into an observable, schedulable execution platform:

| Capability | What it shows | User benefit |
| --- | --- | --- |
| Product dashboard home | Runtime Surface, product map, workflow, and use cases | Understand the product and enter a real execution path from one place |
| Product map | Command, Control, Execution, Knowledge, and Event layers | See how entry points, orchestration, workers, context, and results connect |
| DAG orchestration | `run/submit` creates subtasks and schedules dependency waves | Break down, track, and recover complex work |
| Unified control plane | Dashboard and gRPC TaskService share task state | Keep task state consistent across entry points |
| Distributed workers | Registration, capabilities, heartbeats, and load-aware scheduling | Scale execution capacity around model and tool capabilities |
| Search and memory | Text + Semantic + AST retrieval with TiKV/Qdrant/PostgreSQL memory | Give coding agents reusable context across repositories |
| Reliable deployment | Rust Gateway, NATS, Docker, in-memory fallback, and task events | Move from a local workflow to a worker cluster without changing the product surface |

## Quick Start

### 1. Install prerequisites

- Rust 1.75+ (stable)
- Python 3.9+
- Bun (only for the optional OMP extension)
- [Grok Build CLI](https://docs.x.ai/build/overview) (default worker executor)
- Docker Compose (for the default app and its storage services)

Install Grok Build and provide an xAI API key for the default worker:

```bash
curl -fsSL https://x.ai/cli/install.sh | bash
export XAI_API_KEY=your-key
```

### 2. Start the local workflow

```bash
git clone https://github.com/JameryW/UltimateCoders.git
cd UltimateCoders
docker compose -f docker/docker-compose.yml -f docker/docker-compose.local.yml --profile app up --build
```

The Compose app starts the Gateway, Dashboard API and UI, NATS, storage, and Workers. The optional OMP extension can be started separately with `./run-omp.sh`.

The browser entry points are:

| Entry point | Use |
| --- | --- |
| `http://localhost:8081/` | Product home: capabilities and execution path |
| `http://localhost:8081/dashboard` or `http://localhost:8081/#/dashboard` | Operations dashboard: tasks, workers, events, scheduler, search, files, and metrics |

Optional OMP commands:

```text
/uc submit <description>    Submit a task
/uc status                  Show task status
/uc pause <task-id>         Pause a task
/uc resume <task-id>        Resume a task
/uc cancel <task-id>        Cancel a task
```

### 3. Start other modes

```bash
# Distributed cluster: NATS + gRPC + multiple workers + optional OMP
./run-cluster.sh --workers 2

# Standalone Gateway: in-memory fallback or external storage
./run-gateway.sh up

# Gateway + local storage containers
./run-gateway.sh up --docker
```

For build, test, configuration, and external Git deployment details, see [Building](#building), [Configuration](#configuration), and [Distributed Worker + External Git Deployment](#distributed-worker--external-git-deployment).

## Technical Architecture

![UltimateCoders technical architecture](docs/screenshots/execution-architecture.png)

The diagram follows one task through the system: the Dashboard accepts requests, the Rust Gateway owns TaskService, DAG scheduling, and worker registration, the Worker Pool executes subtasks, Search + Memory provide repository context, and Task Events return state to the Dashboard and API consumers.

| Layer | Components | Responsibility |
| --- | --- | --- |
| Interaction | Web Dashboard; optional native OMP | Accept natural-language tasks through gRPC-Web or `/uc` |
| Control plane | Rust Gateway | Task persistence, DAG scheduling, TaskService, EngineService, and WorkerService |
| Execution | Worker Pool | Dispatch by capability, heartbeat, and load to Grok Build, Claude Code, or Codex |
| Knowledge | Search + Memory | Combine Text, Semantic, and AST retrieval with TiKV, Qdrant, and PostgreSQL memory |
| Observability | Task Events | Broadcast submitted, running, completed, and failed states to Dashboard and API |

## Product Preview

The product home (`/`) explains capabilities and the execution path. The operations dashboard (`#/dashboard`) is the live monitoring and task control surface.

### Dashboard home and live entry points

The product home is the navigation layer between product understanding and real execution:

- **Runtime Surface**: show Gateway status, version, task count, and WatchTask state through the existing gRPC-Web connection.
- **Product Map**: explain the responsibilities, protocols, and benefits of the Web, Control, Execution, Knowledge, and Event planes.
- **Live handoff**: open `#/dashboard` to submit tasks and inspect the real Gateway state.

Local Vite preview: `http://127.0.0.1:4176/`; operations dashboard: `http://127.0.0.1:4176/#/dashboard`.

### Product capabilities

![UltimateCoders product capabilities](docs/screenshots/product-capabilities.png)

This overview covers DAG orchestration, capability-aware workers, Hybrid Search + Memory, event-driven recovery, and the path from a local workflow to a cluster.

### Product use cases

![UltimateCoders product use cases](docs/screenshots/product-scenarios.png)

UltimateCoders targets large-repository changes, parallel delivery, incident diagnosis, and local-to-cluster expansion. The recurring benefits are reusable context, observable execution, and scalable capacity.

## Runtime and architecture

The product dashboard (Vite + React) is available at `http://localhost:5173/` in development. Its root route is the product overview; the operations dashboard is at `#/dashboard`.

See [docs/architecture.md](docs/architecture.md) for the detailed architecture reference. The runtime can be read in five layers:

| Layer | Responsibility | Main interfaces |
| --- | --- | --- |
| Web | Dashboard task entry and monitoring | gRPC-Web, TaskService |
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
`UC_CODING_AGENT=local-harness` for a smaller local OpenAI-compatible model,
or `UC_CODING_AGENT=claude-code` / `codex` when a deployment uses those CLIs.

### Multi-Worker Distributed Architecture

Multiple NATS Worker processes can collaborate on a single task:

- **NATS queue group** — each subtask delivered to exactly one worker via `uc.subtask.execute`
- **Affinity placement** — a worker that binds its own per-worker subject (`uc.subtask.execute.w.<worker_id>`) and reports its recently-touched files on the gateway heartbeat is targeted first when a subtask's `file_constraints` overlap that recent work. The shared subject stays bound as overflow, so placement can never strand a node
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
| `dashboard/` | Vite + React product homepage and operations dashboard |
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

The `uc-grpc-server` gateway enables the scheduler runtime by default, so
the standard launcher and Docker builds execute jobs declared in
`uc.scheduler.yaml`. The lower-level `uc-engine` library keeps scheduler
support feature-gated for consumers that do not need runtime scheduling.

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
# Build and start the complete local app against this checkout, including
# the React Dashboard UI and a writable Git workspace for workers.
docker compose -f docker/docker-compose.yml -f docker/docker-compose.local.yml --profile app up --build
# React UI: http://localhost:8081
# Dashboard API: http://localhost:8080/dashboard/

# Start all storage backends
docker compose -f docker/docker-compose.yml up -d

# Stop everything
docker compose -f docker/docker-compose.yml down

# Stop and remove volumes
docker compose -f docker/docker-compose.yml down -v
```

For external Git workers (`UC_REPO_URL` set), use only the base compose file:
their `/workspace` stays on the persistent `worker_workspace` volume. The
local override bind-mounts this checkout at `/workspace` for both Python
services and configures Git's `safe.directory` for that mount and its generated
worktrees on Docker Desktop. Do not combine it with external Git sync.

For a real coding task, start the configured LLM service before submitting.
With Ollama on the Docker host, `ollama serve` must be reachable at
`host.docker.internal:11434` from the workers, and `UC_OPENROUTER_MODEL`
must name an installed Ollama model when Codex uses the local provider.
The Gateway's storage health check does not test model generation.

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

Ten independent workflows run on pushes and pull requests targeting `main`. All but `ci-scripts.yml` are path-filtered, so they only run when one of the listed paths changes; the workflow files themselves are the source of truth:

| Workflow | Trigger paths | Checks |
|----------|--------------|--------|
| **Rust CI** (`ci-rust.yml`) | `crates/**`, `Cargo.toml`, `Cargo.lock`, `docker/docker-compose.yml` | check, clippy, fmt, test (3 feature combos), postgres integration |
| **Python CI** (`ci-python.yml`) | `python/**`, `tests/**`, `pyproject.toml`, `dashboard/**`, `crates/uc-python/**`, `crates/uc-types/**`, `crates/uc-engine/**`, `crates/uc-grpc/**`, `Cargo.toml`, `Cargo.lock` | ruff lint, dashboard build, pytest (3.9 + 3.12) |
| **Dashboard CI** (`ci-dashboard.yml`) | `dashboard/**` | typecheck, build |
| **TypeScript CI** (`ci-typescript.yml`) | `packages/uc-orchestrator/**`, `vendor/oh-my-pi/packages/mnemopi/**`, `vendor/oh-my-pi/packages/coding-agent/**` | `bun test` for each package |
| **Scripts CI** (`ci-scripts.yml`) | *(no paths filter)* | spec-refs, tasks-refs, ruff lint |
| **Trellis CI** (`ci-trellis.yml`) | `.trellis/scripts/**`, `tests/python/test_task_finish_fallback.py`, `tests/python/test_archive_repoints_refs.py` | framework-scripts tests |
| **Journal CI** (`ci-journal.yml`) | `.trellis/workspace/**`, `.trellis/scripts/add_session.py`, `scripts/check-journal-ledger.py`, `tests/python/test_check_journal_ledger.py` | journal-ledger check + tests |
| **Codex Issue-Flow CI** (`ci-codex-flow.yml`) | `.agents/skills/**`, `AGENTS.md`, `docs/agents/domain.md`, `docs/agents/issue-tracker.md`, `docs/agents/mattpocock-skills.md`, `docs/agents/triage-labels.md`, `docs/workflows/codex-issue-flow.md`, `scripts/check-codex-issue-flow.py` | issue-workflow wiring validation |
| **README CI Table CI** (`ci-readme-ci-table.yml`) | `README.md`, `README.zh-CN.md`, `.github/workflows/**`, `scripts/check-readme-ci-table.py`, `tests/python/test_check_readme_ci_table.py` | reconciles this table against the workflow YAML |
| **Workflow Inputs CI** (`ci-workflow-inputs.yml`) | `.github/workflows/**`, `scripts/check-workflow-inputs.py`, `tests/python/test_check_workflow_inputs.py` | checks that every file a workflow's `run:` steps name is covered by its `paths` |

Every path-filtered workflow's own YAML file also matches its `paths` (named directly, or through `.github/workflows/**`), so editing a workflow re-runs it, and every workflow supports manual dispatch. The PostgreSQL-backed suite runs on every PR; storage integration tests only run on `main` pushes or manual dispatch (requires Docker Compose infra).

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
| `UC_CODING_AGENT` | `grok-build` | Worker coding agent (`grok-build`/`grok`, `local-harness`, `claude-code`, or `codex`) |
| `XAI_API_KEY` | - | xAI API key for the default Grok Build worker agent |
| `ANTHROPIC_API_KEY` | - | Anthropic API key for Claude Code calls |
| `OPENAI_API_KEY` | - | OpenAI API key for Codex calls |
| `UC_CODEX_OPENROUTER` | `false` | Enable OpenRouter as the Codex CLI provider (requires `UC_CODING_AGENT=codex`) |
| `UC_OPENROUTER_MODEL` | `stealth/ox-alpha` | OpenRouter model slug used by the opt-in Codex provider |
| `UC_CODEX_WEB_SEARCH` | `disabled` | Codex native web-search mode; keep `disabled` for `stealth/ox-alpha`, which rejects that OpenAI-specific tool |
| `UC_OPENROUTER_REASONING_EFFORT` | `low` | Codex reasoning effort; `stealth/ox-alpha` requires reasoning to be enabled |
| `OPENROUTER_API_KEY` | - | OpenRouter API key used by the opt-in Codex provider |
| `UC_SANDBOX_ENV_EXTRA` | - | Extra env vars passed to agent subprocesses, comma-separated (`*` suffix = prefix match). Agent subprocesses get a deny-by-default allowlist — only base system vars, `UC_*`, proxy vars, and the per-agent credentials listed above are inherited from the host environment. Use this to widen the list; it is logged at worker startup. |

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
ruff check python/ tests/ scripts/

# UC Orchestrator
cd packages/uc-orchestrator && npx tsc --noEmit
```

## License

MIT
