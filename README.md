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

### 核心流程

```text
用户命令
   │
   ├─ Dashboard Command Deck（能力说明）
   ├─ TUI 命令栏 / OMP
   │
   ▼
OMP UC Extension → TaskService.SubmitTask
   │
   ▼
任务拆解 → DAG 调度 → WorkerService / NATS 分发
   │                         │
   └──────── 实时事件 ←───────┘
                 │
                 ▼
       OMP 输出 / TUI 输出 / Dashboard 状态 / Memory
```

## Quick Start

### Prerequisites

- Rust 1.75+ (stable)
- Python 3.9+
- Bun (for OMP runtime)
- [Grok Build CLI](https://docs.x.ai/build/overview) (for the default local worker)
- Docker and Docker Compose (optional, for storage backends)

For a local worker, install Grok Build and provide an xAI API key:

```bash
curl -fsSL https://x.ai/cli/install.sh | bash
export XAI_API_KEY=your-key
```

### 单机模式（推荐）

```bash
# 启动 OMP + gRPC server（gRPC server 默认启动，LocalWorker 懒启动）
./run-omp.sh

# 跳过 gRPC server
./run-omp.sh --no-server

# 首次运行需构建 Python 包
./run-omp.sh --build
```

### 分布式集群模式

```bash
# 一键启动：NATS + gRPC server + N workers + OMP
./run-cluster.sh

# 自定义 worker 数量（默认 2）
./run-cluster.sh --workers 4

# 仅后端（不启动 OMP，适合 headless 场景）
./run-cluster.sh --no-omp

# 用 Docker 提供存储后端（TiKV + Qdrant + PostgreSQL + NATS）
./run-cluster.sh --docker

# 停止所有集群进程
./run-cluster.sh --stop
```

### 独立部署模式（容器化 gateway）

```bash
# 仅 gateway 容器，存储走内存 fallback（或外部存储，见下方 env）
./run-gateway.sh up

# gateway + 本地存储容器（TiKV + Qdrant + PostgreSQL + NATS）
./run-gateway.sh up --docker

# 查看状态 / 日志 / 停止
./run-gateway.sh status
./run-gateway.sh logs
./run-gateway.sh down [--docker]

# 独立 gateway + OMP（OMP 连接容器 gateway）
./run-omp.sh --standalone

# 独立集群：容器 gateway + 存储 + 本机 workers
./run-cluster.sh --standalone --workers 2
```

外部存储部署（默认模式，无 `--docker`）：导出 env 指向远端后端，空值 = 内存 fallback。

```bash
export UC_TIKV_PD_ENDPOINTS=pd.example:2379
export UC_QDRANT_URL=http://qdrant.example:6334
export UC_PG_URL=postgresql://user:pass@pg.example:5432/ultimate_coders
export UC_NATS_URL=nats://nats.example:4222
./run-gateway.sh up
```

### Docker Compose（存储后端）

```bash
docker compose -f docker/docker-compose.yml up -d
```

This starts TiKV, Qdrant, PostgreSQL, and NATS. See [Configuration](#configuration) for connection details.

### 2. Build the Rust Core

```bash
cargo check          # Verify compilation
cargo test           # Run all tests (with in-memory fallbacks)
```

### 3. Build the Python Package

```bash
python -m venv .venv
# PowerShell: .venv\\Scripts\\Activate.ps1
# Bash/WSL:    source .venv/bin/activate
python -m pip install -e ".[test]"  # Runtime + test deps and Rust extension
```

The editable install targets `crates/uc-python/Cargo.toml` and uses a
platform-matched vendored `protoc` when a system `protoc` is not installed.
This keeps a clean Windows checkout consistent with Docker and CI.

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
# Start OMP with UC extension (gRPC server starts by default)
./run-omp.sh

# Skip gRPC server (OMP only)
./run-omp.sh --no-server
```

The UC Orchestrator runs inside OMP's terminal UI. Use `/uc submit <description>` to submit tasks, `/uc status` to check progress, and `/uc cancel/pause/resume` for control. Keyboard shortcuts: **Ctrl+T** for subtask tree overlay, **Ctrl+Shift+T** for task list.

The OMP extension also registers LLM-callable tools:
- `uc_task` — Task lifecycle: submit/cancel/pause/resume/status
- `uc_worker` — Worker management: list workers / check load/capacity/heartbeat, `scale` the cluster to a target count (docker compose), or `deregister` a stale worker from the registry
- `uc_memory` — Shared layered memory: read/write/search/delete (task/project/global scopes)
- `uc_search` — Hybrid index search (text + semantic + AST) across indexed repos
- `uc_index` — Index management: index_repo / list_repos / get_state / remove_index
- `uc_file` — File operations: list_dir / get_file

The product dashboard (Vite + React) is available at `http://localhost:5173/` with the default Vite config. Its root route is the product overview; open `#/tui` for the real OMP PTY terminal connected to the Gateway TaskService over gRPC-Web. The current local handoff uses port `4176` (`http://127.0.0.1:4176/`).

## Architecture

See [docs/architecture.md](docs/architecture.md) for the full architecture document.

```
+-------------------+     +-------------------------------+     +---------------+
|   Python Worker   |     |  OMP + UC Extension           |     | TUI Web Shell |
|  (NATS Worker /   |     |  +-------------------------+  |     +-------+-------+
|   local fallback) |     |  │ Orchestrator Core       │  |     | (Vite/React)  |
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

```
ultimate-coders/
├── Cargo.toml                # Workspace root
├── pyproject.toml            # Maturin build config
├── run-omp.sh                # Start OMP with UC extension (primary interface)
├── run-cluster.sh            # Start local distributed cluster (NATS + workers)
├── run-gateway.sh            # Manage standalone containerized gateway
├── crates/
│   ├── uc-types/             # Shared types + EngineApi trait
│   ├── uc-engine/            # Core engine (LocalEngine implementation)
│   ├── uc-grpc/              # gRPC server/client + proto + broadcast channel + NATS integration
│   ├── uc-grpc-server/       # Standalone gRPC server binary
│   └── uc-python/            # PyO3 Python binding
├── packages/
│   └── uc-orchestrator/      # OMP extension — task orchestration + rich TUI
│       ├── src/
│       │   ├── extension.ts  # Extension entry (commands, shortcuts, renderers)
│       │   ├── orchestrator/ # Core orchestration logic
│       │   │   ├── orchestrator.ts   # Main orchestrator (submit, cancel, pause, resume, DAG waves)
│       │   │   ├── scheduler.ts      # DAG builder, wave splitter, circuit breaker
│       │   │   ├── grpc-bridge.ts    # gRPC-Web client for TaskService (submit, watch, control)
│       │   │   ├── memory-bridge.ts  # LLM tool: uc_memory (read/write/search/delete)
│       │   │   ├── task-bridge.ts    # LLM tool: uc_task (submit/cancel/pause/resume/status)
│       │   │   ├── index-bridge.ts   # LLM tool: uc_index (index_repo/list_repos/get_state/remove_index)
│       │   │   ├── file-bridge.ts    # LLM tool: uc_file (list_dir/get_file)
│       │   │   ├── worker-bridge.ts  # LLM tool: uc_worker (list/status/scale/deregister)
│       │   │   ├── task-store.ts     # SQLite-backed task persistence
│       │   │   ├── control-signal-subscriber.ts  # gRPC stream control signals
│       │   │   └── events.ts         # Typed event emitter (orchestration ↔ UI)
│       │   ├── ui/           # pi-tui components
│       │   │   ├── progress-widget.ts       # Live subtask progress
│       │   │   ├── subtask-tree-overlay.ts  # Ctrl+T overlay
│       │   │   ├── task-list-overlay.ts     # Ctrl+Shift+T overlay
│       │   │   ├── task-result-renderer.ts  # Custom message renderer
│       │   │   ├── error-format.ts          # Error message formatting
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
│       ├── repo_config.py    # uc.repos.yaml loader + RepoScanner auto-discovery
│       ├── nats_worker.py    # NATS consumer/producer bridge
│       ├── search/           # SearchQuery builder
│       ├── memory/           # Memory read/write interface
│       └── config.py         # Configuration loading
├── dashboard/                # Vite + React product dashboard + TUI terminal
├── docker/                   # Docker configs + Dockerfiles + compose + scheduler config
├── tests/python/             # Python unit tests
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
