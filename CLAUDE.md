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

# Distributed deployment (Docker)
docker compose --profile gateway up   # Start gateway (Rust gRPC server)
docker compose --profile worker up --scale worker=3  # Start 3 workers
docker compose --profile app up       # Start all services (gateway + orchestrator + workers)

# Gateway-only standalone (storage external, no TiKV/Qdrant/PG/NATS started)
# Inject external storage addresses via env/.env; empty = in-memory fallback
docker compose -f docker/docker-compose.gateway.yml up
```

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
