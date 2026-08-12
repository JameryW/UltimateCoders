# UltimateCoders

[![Rust CI](https://github.com/JameryW/UltimateCoders/actions/workflows/ci-rust.yml/badge.svg)](https://github.com/JameryW/UltimateCoders/actions/workflows/ci-rust.yml)
[![Python CI](https://github.com/JameryW/UltimateCoders/actions/workflows/ci-python.yml/badge.svg)](https://github.com/JameryW/UltimateCoders/actions/workflows/ci-python.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Distributed AI Coding System with shared layered memory and multi-repo hybrid retrieval (Text + Semantic + AST).

The UC Orchestrator runs as an oh-my-pi (OMP) extension, providing rich terminal interaction with subtask progress widgets, overlays, custom message rendering, and LLM-callable memory tools. The Python Worker/Sandbox defaults to the [xAI Grok Build](https://github.com/xai-org/grok-build) terminal coding agent (`grok`) for subtask execution; Claude Code and Codex remain explicit compatibility options. OMP's local `runSubprocess` path is separate and still handles its own local decomposition/execution flow. The Rust core handles indexing, search, memory, and scheduling. A broadcast channel delivers real-time task events to TUI and API consumers.

## 主要功能 / Key Features

- **DAG 任务编排**：将自然语言任务拆解为可观测的 subtasks，按依赖关系分波次调度，并持续回传 submitted、running、completed、failed 等状态。
- **产品首页 + 运营 Dashboard + TUI**：React + gRPC-Web 的 `/` 展示产品能力和执行链；保留完整旧版运营 dashboard 于 `#/dashboard`（任务、Worker、事件、调度、检索、文件、指标），`#/tui` 通过 WebSocket 接入真实 OMP 会话。
- **TUI / OMP 共享命令层**：`run/status/tasks/workers/search/logs` 共用同一套 UC 语义，命令结果直接回显在终端并同步到 TaskEvent 流。
- **分布式 Worker**：Worker 通过 `WorkerService` 注册、心跳和能力声明，由 Gateway 按能力与负载调度；NATS 负责跨进程 subtask 分发。
- **Rust 高性能核心**：Engine、Task、Dashboard、Worker 四类服务统一暴露 gRPC/gRPC-Web 接口，支持任务恢复、事件广播和内存 fallback。
- **跨仓库混合检索**：同一查询支持 Text、Semantic、AST 三种检索能力，可面向多个 Git 仓库索引和搜索。
- **分层 Memory**：短期记忆、长期语义记忆和结构化元数据分别对接 TiKV、Qdrant、PostgreSQL；依赖不可用时可退回内存模式。
- **多种部署方式**：支持本机 OMP、Docker Gateway、Docker Compose 以及多 worker 集群；Worker 可选择 Grok Build、Claude Code 或 Codex 执行器。

## 产品特性与优势 / Product Highlights

UltimateCoders 把“终端里的 AI Coding”扩展成可观测、可调度、可扩展的执行平台：

| 产品能力 | 展示内容 | 直接收益 |
| --- | --- | --- |
| Dashboard 产品首页 | Runtime Surface、OMP ↔ UC Loop、Command Deck、典型场景 | 先理解全产品，再从同一页面进入真实执行入口 |
| 产品分层图 | Command、Control、Execution、Knowledge、Event 五层 | 直观看到入口、编排、Worker、上下文和结果回流如何连接 |
| OMP 原生交互 | WebSocket PTY、实时终端输出、断线重连 | 保留熟悉的终端体验，同时接入 Web 控制台 |
| DAG 编排 | `run/submit` 创建任务，拆分 subtasks 并按依赖分波次执行 | 复杂任务可拆解、可追踪、可恢复 |
| 统一控制面 | TUI、OMP 命令和 gRPC TaskService 共用命令语义 | 不同入口看到同一份任务状态，减少操作割裂 |
| 分布式 Worker | Worker 注册、能力声明、心跳和负载感知调度 | 根据模型/工具能力扩展执行容量，并支持 Claude Code/Codex/Grok Build |
| 检索与记忆 | Text + Semantic + AST 混合检索，TiKV/Qdrant/PostgreSQL 分层 Memory | 让 Coding Agent 获得跨仓库上下文，结果更稳定、更可复用 |
| 可靠部署 | Rust Gateway、NATS、Docker、内存 fallback 和任务事件广播 | 从本地单机平滑演进到多 Worker 集群 |

## Quick Start

### 1. 安装依赖

- Rust 1.75+（stable）
- Python 3.9+
- Bun（OMP runtime）
- [Grok Build CLI](https://docs.x.ai/build/overview)（默认 Worker 执行器）
- Docker Compose（可选，用于 TiKV、Qdrant、PostgreSQL 和 NATS）

安装默认的 Grok Build Worker，并设置 xAI API key：

```bash
curl -fsSL https://x.ai/cli/install.sh | bash
export XAI_API_KEY=your-key
```

### 2. 启动本机工作流

```bash
git clone https://github.com/JameryW/UltimateCoders.git
cd UltimateCoders
./run-omp.sh --build
```

`--build` 会先构建 Python 包；启动脚本默认拉起 gRPC Gateway、FastAPI Dashboard、Vite 产品界面和 OMP。若只需要 OMP，可使用 `./run-omp.sh --no-server`。

启动后可从三个入口进入：

| 入口 | 用途 |
| --- | --- |
| `http://localhost:5173/` | 产品首页：能力总览、执行链和 Command Deck |
| `http://localhost:5173/#/dashboard` | 旧版运营 Dashboard：任务、Worker、事件、调度、检索、文件和指标 |
| `http://localhost:5173/#/tui` | 真实 OMP PTY 终端：执行共享 UC 命令并查看实时输出 |

常用命令：

```text
/uc submit <description>    提交任务
/uc status                  查看任务状态
/uc pause <task-id>         暂停任务
/uc resume <task-id>        恢复任务
/uc cancel <task-id>       取消任务
```

### 3. 启动其他模式

```bash
# 分布式集群：NATS + gRPC + 多个 Worker + OMP
./run-cluster.sh --workers 2

# 独立 Gateway：内存 fallback 或外部存储
./run-gateway.sh up

# Gateway + 本地存储容器
./run-gateway.sh up --docker
```

更多构建、测试、配置和外部 Git 部署说明见下方 [Building](#building)、[Configuration](#configuration) 和 [Distributed Worker + External Git Deployment](#distributed-worker--external-git-deployment)。

## 页面预览 / Screenshots

产品首页（`/`）展示完整能力、执行链、OMP 双向交互和 Command Deck；旧版运营 dashboard（`#/dashboard`）继续提供详细监控面板；`#/tui` 是连接真实 OMP PTY、Gateway TaskService 的交互式终端。下面的截图和视频覆盖这些入口。

### Dashboard 首页与可操作展示

首页不是旧版监控 Dashboard 的替代截图，而是产品能力总览和真实入口之间的导航层：

- **Runtime Surface**：通过现有 gRPC-Web 连接显示 Gateway 状态、版本、任务数量和 WatchTask 状态。
- **OMP ↔ UC Loop**：展示 `OMP terminal → UC Extension → Rust Gateway → Worker → TaskEvent` 的完整双向链路；点击阶段会切换对应的交互 trace。
- **Command Deck**：可切换查看 `run`、`status`、`tasks`、`workers`、`search`、`logs` 对应的服务路径、示例输入和典型返回。
- **Product Map**：用五层视图解释 Command surface、Control plane、Execution plane、Knowledge plane 和 Event plane 的职责、协议与优势。
- **Live handoff**：从 Command Deck 或 OMP 流程直接进入 `#/tui`，执行仍由真实 WebSocket + gRPC-Web 通路完成。
- **单会话接管**：OMP PTY 默认保持单标签页连接；如果打开多个 TUI 标签，当前页会明确显示占用状态，并可用 `Take over` 接管同一会话。

本地查看：`http://127.0.0.1:4176/`；真实终端：`http://127.0.0.1:4176/#/tui`。

### 全产品能力总览

![UltimateCoders product capabilities](docs/screenshots/product-capabilities.png)

这张总览图展示产品的六个核心面：DAG 编排、能力感知 Worker、Hybrid Search + Memory、OMP 原生交互、事件驱动恢复和从单机到集群的部署路径。

### 典型场景与直接收益

![UltimateCoders product use cases](docs/screenshots/product-scenarios.png)

UltimateCoders 面向大型仓库改造、并行交付、线上问题诊断和本地到集群扩展四类高频场景，核心收益是上下文可复用、执行可观测、容量可弹性扩展。

### 执行架构

![UltimateCoders execution architecture](docs/screenshots/execution-architecture.png)

任务从 OMP/TUI 进入 Rust Gateway，经 TaskService 和 DAG Scheduler 分发到 Worker Pool，同时通过 Search/Memory 获取上下文，最终由统一 TaskEvent 广播到 TUI 和 API。

### TUI / OMP 终端

`#/tui` 是 OMP 交互入口，顶部显示会话状态，命令栏用于执行共享 UC 命令；命令回显和 Gateway 返回结果会保留在终端区。在 Docker/WSL 或已安装 PTY/WebSocket 运行时的环境中，还会继续显示 OMP 的实时输出。产品首页负责解释能力和链路，真实执行统一进入这个终端。

#### 完整产品走查

![UltimateCoders TUI full product walkthrough](docs/screenshots/tui-terminal.png)

画面集中展示 OMP 连接、Gateway 状态、Worker 能力、检索入口、事件日志、真实任务提交和 TaskService 查询结果。

#### 命令与平台能力目录

![UltimateCoders TUI command catalog](docs/screenshots/tui-command-catalog.png)

通过同一命令栏可进入 `status`、`tasks`、`workers`、`search`、`logs`、`submit/run`、暂停/恢复/取消等产品能力；首页 Command Deck 同步解释这些命令对应的服务路径。

#### DAG 任务提交

![UltimateCoders TUI DAG task submission](docs/screenshots/tui-task-dag.png)

自然语言任务通过真实 TaskService 提交，返回 task ID 后进入 DAG 调度链路。

### 功能演示视频

[下载全产品能力演示视频](docs/videos/ultimatecoders-product-showcase.mp4)

视频先展示产品能力总览、典型场景和执行架构，再进入真实页面走查：OMP WebSocket 连接成功 → 查询 Gateway 状态、Worker、Search 和 Logs → 在 TUI 中输入并执行 `run` 任务 → 返回真实 task ID 并进入 DAG → 用 `tasks` 查询 TaskService 数据。

另有 [TUI 交互细节视频](docs/videos/ultimatecoders-tui-demo.mp4)，用于查看命令栏、终端回显和 OMP WebSocket 连接的细节。

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
