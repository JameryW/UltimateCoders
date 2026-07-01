# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**UltimateCoders** — 分布式 AI Coding 系统，分为 **Orchestrator/Gateway** 和 **Distributed Worker** 两个独立部署单元。Gateway 以 OMP Extension + Rust gRPC Server 为核心，Worker 以 Python 为执行引擎，通过 NATS 消息 + gRPC WorkerService 通信，共享分层 Memory，并集成多 Git 仓库的混合检索能力（Text + Semantic + AST）。

## Architecture

- **Orchestrator/Gateway** (独立部署):
  - **OMP Extension** (TypeScript): UC Orchestrator — 任务编排 + UI 组件 + LLM tools + Coding Agent
  - **Rust gRPC Server** (5 crates): uc-types, uc-engine, uc-grpc, uc-grpc-server, uc-python
    - EngineService / TaskService / DashboardService / **WorkerService**
    - WorkerRegistry — 能力感知调度（capabilities + load matching）
- **Distributed Worker** (独立部署, 可多实例扩缩容):
  - **Python Worker/Sandbox**: gRPC WorkerService 注册 + NATS subtask 执行
- **桥接**: PyO3 FFI (本地) + gRPC-Web (OMP→Rust) + gRPC WorkerService (Worker→Gateway) + NATS (subtask dispatch)
- **存储**: TiKV (短期 Memory) + Qdrant (长期 Memory + 语义检索) + PostgreSQL (结构化元数据)

## Repository Structure

```
ultimate-coders/
├── run-omp.sh               # Start OMP with UC extension (primary entry point)
├── run-gateway.sh           # Manage standalone containerized gateway
├── run-cluster.sh           # Start local distributed cluster (NATS + workers)
├── Cargo.toml               # Rust workspace root
├── pyproject.toml            # Maturin build config
├── crates/                   # Rust core engine (Gateway)
│   ├── uc-types/             #   Shared types + EngineApi trait
│   ├── uc-engine/            #   LocalEngine + sandbox + scheduler
│   ├── uc-grpc/              #   gRPC server/client + proto + broadcast + WorkerRegistry
│   ├── uc-grpc-server/       #   Standalone gRPC server binary
│   └── uc-python/            #   PyO3 Python binding
├── packages/
│   └── uc-orchestrator/     # OMP extension (TypeScript)
│       └── src/
│           ├── extension.ts  #   Commands, shortcuts, renderers
│           ├── orchestrator/ #   Core logic + events + bridges
│           ├── ui/           #   pi-tui components
│           └── agents/       #   LLM role prompts
├── python/ultimate_coders/  # Python layer (Worker)
│   ├── agent/               #   Worker, Sandbox, Scheduler
│   ├── dashboard/           #   FastAPI metrics + SSE
│   ├── search/              #   SearchQuery builder
│   └── memory/              #   Memory read/write
├── dashboard/               # Vite + React web dashboard
├── docker/                  # Dockerfiles + compose + configs
│   ├── docker-compose.yml   #   gateway + worker separated services
│   ├── Dockerfile           #   Python worker image
│   └── Dockerfile.grpc      #   Rust gateway image
├── tests/python/            # Python tests
└── vendor/oh-my-pi/         # OMP upstream (submodule)
```

## Build & Run

```bash
cargo check                  # Check all crates
cargo test -p uc-engine      # Run engine tests (no features)
maturin develop              # Build Rust extension + install
cargo run -p uc-grpc-server  # Start gRPC gateway server
./run-omp.sh                 # Start OMP with UC extension (primary)
./run-omp.sh --server        # Also start gRPC server in background
./run-omp.sh --build         # Ensure Python package is built first
./run-omp.sh --standalone    # Standalone: gateway in a container (in-memory/external storage)
./run-omp.sh --standalone --docker  # Standalone gateway + local storage containers

# Standalone gateway container manager (storage external or in-memory fallback)
./run-gateway.sh up          # Gateway container only (no storage containers)
./run-gateway.sh up --docker # Gateway + local storage (TiKV/Qdrant/PG/NATS)
./run-gateway.sh down [--docker]  # Stop (add --docker to also stop storage)
./run-gateway.sh status|logs

# Distributed cluster (local)
./run-cluster.sh                          # NATS + gRPC + N workers + OMP
./run-cluster.sh --standalone --workers 2 # Container gateway + storage + host workers

# Distributed deployment (Docker, raw compose)
docker compose --profile gateway up   # Start gateway (Rust gRPC server)
docker compose --profile worker up --scale worker=3  # Start 3 workers (SAME HOST only)
docker compose --profile app up       # Start all services (gateway + orchestrator + workers)

# Gateway-only standalone (storage external, no TiKV/Qdrant/PG/NATS started)
# Inject external storage addresses via env/.env; empty = in-memory fallback
docker compose -f docker/docker-compose.gateway.yml up   # or: ./run-gateway.sh up
```

### Distributed (cross-host) scaling

`docker compose --scale worker=N` only scales workers on the **same host**
— the gateway shells out to the local `docker.sock`, which controls the
local docker daemon. True cross-host scaling requires docker swarm mode, a
remote docker context, or a per-host gateway (future work). The
distributed worker + external-git design (below) is already cross-host-safe
at the data level: each host runs its own compose and clones from the same
external git remote, and git merge-time arbitration reconciles concurrent
edits.

## Deployment Architecture

```
┌─────────────────────────────────────────────────┐
│  Orchestrator/Gateway (独立部署)                   │
│                                                   │
│  OMP Extension ──gRPC-Web──→ Rust gRPC Server    │
│  (TypeScript)                  ├─ EngineService   │
│                                ├─ TaskService     │
│                                ├─ DashboardService│
│                                └─ WorkerService   │
│                                      │            │
│                                  WorkerRegistry   │
└──────────────────────────┬──────────┬─────────────┘
                           │          │
                    gRPC   │          │ NATS
                  (register)│         │ (dispatch)
                           │          │
┌──────────────────────────┴──────────┴─────────────┐
│  Distributed Workers (独立部署, 可多实例)             │
│                                                     │
│  Python Worker                                      │
│  ├─ gRPC: RegisterWorker + Heartbeat + Deregister   │
│  └─ NATS: subscribe uc.subtask.execute              │
└─────────────────────────────────────────────────────┘
```

## Distributed Worker + External Git Deployment

Workers run containerized and sync code from an **external git remote**
(GitHub/GitLab), which is the unified source of truth across hosts.
Push and merge arbitration are **opt-in** (off by default unless
`UC_REPO_URL` is set — without it the legacy local-only workspace mode
is used).

### Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `UC_REPO_URL` | _(empty)_ | External git remote URL. Empty = local-only workspace (legacy). |
| `UC_REPO_BASE_BRANCH` | `main` | Base branch workers branch off and the arbiter merges into. |
| `UC_GIT_TOKEN` | _(empty)_ | PAT injected via `GIT_ASKPASS` (never on the URL / in args). |
| `UC_GIT_FETCH_ON_ACQUIRE` | `true` | `git fetch` before each worktree acquire. |
| `UC_GIT_PUSH_ON_RELEASE` | `false` | Push the `uc/subtask/<id>` branch back to the remote on release. |
| `UC_GIT_MERGE_ARBITRATE` | _(env)_ | When set, the Orchestrator's `MergeArbiter` merges completed subtask branches into `origin/main` and pushes `main`. |

### Flow

1. **Clone** — on first start, each worker clones `UC_REPO_URL` into a
   persistent volume (`worker_workspace:/workspace`); subsequent starts
   reuse the clone.
2. **Worktree isolation** — each subtask acquires a git worktree branched
   off `origin/<base_branch>`, so concurrent subtasks never collide.
3. **Push** — on release, the worker (when `UC_GIT_PUSH_ON_RELEASE=true`)
   pushes its branch `uc/subtask/<id>` to the remote. Workers **never**
   touch `main`.
4. **Merge arbitration** — the Orchestrator's `MergeArbiter` (Phase 2,
   when `UC_GIT_MERGE_ARBITRATE` is set) fetches each `uc/subtask/<id>`
   branch, merges it into `origin/main` (escalating to the in-memory
   `ConflictResolver` on git conflict), and pushes `main`. The arbiter is
   the **only** writer of `main`.

### Conflict model

- `DistributedConflictDetector` is an **advisory** in-process scheduling
  hint (it is NOT a distributed lock — it never touches the network for
  locking). It reduces the probability of same-process file overlap.
- The **authoritative** cross-worker conflict point is git merge-time
  (`MergeArbiter`). Two workers on different hosts editing the same file
  are reconciled when their subtask branches merge into `main`.

## Key Types

- `EngineApi` trait (uc-types/src/engine.rs) — unified engine contract
- `EngineError` (uc-types/src/error.rs) — shared error types
- `SearchQuery/SearchResult` (uc-types/src/search.rs) — hybrid search
- `MemoryKey/MemoryEntry` (uc-types/src/memory.rs) — layered memory
- `Task/Subtask/AgentEvent` (uc-types/src/agent.rs) — orchestration
- `WorkerRegistry/RegisteredWorker` (uc-grpc/src/worker_service.rs) — worker registration + capability discovery

## Repository

- **Remote:** https://github.com/JameryW/UltimateCoders
- **Default branch:** main
