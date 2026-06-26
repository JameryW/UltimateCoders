# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**UltimateCoders** — 分布式 AI Coding 系统，以 OMP Extension 为编排核心，Rust Engine 为后端，Python Worker/Sandbox 为 gRPC fallback 路径，共享分层 Memory，并集成多 Git 仓库的混合检索能力（Text + Semantic + AST）。

## Architecture

- **Rust 核心引擎** (5 crates): uc-types, uc-engine, uc-grpc, uc-grpc-server, uc-python
- **OMP Extension** (TypeScript): UC Orchestrator — 任务编排 + UI 组件 + LLM tools + Coding Agent
- **Python Worker/Sandbox**: gRPC LocalWorkerBridge/NATS fallback 路径
- **桥接**: PyO3 FFI (本地) + gRPC-Web (OMP→Rust)
- **存储**: TiKV (短期 Memory) + Qdrant (长期 Memory + 语义检索) + PostgreSQL (结构化元数据)

## Repository Structure

```
ultimate-coders/
├── run-omp.sh               # Start OMP with UC extension (primary entry point)
├── Cargo.toml               # Rust workspace root
├── pyproject.toml            # Maturin build config
├── crates/                   # Rust core engine
│   ├── uc-types/             #   Shared types + EngineApi trait
│   ├── uc-engine/            #   LocalEngine + sandbox + scheduler
│   ├── uc-grpc/              #   gRPC server/client + proto + broadcast
│   ├── uc-grpc-server/       #   Standalone gRPC server binary
│   └── uc-python/            #   PyO3 Python binding
├── packages/
│   └── uc-orchestrator/     # OMP extension (TypeScript)
│       └── src/
│           ├── extension.ts  #   Commands, shortcuts, renderers
│           ├── orchestrator/ #   Core logic + events + bridges
│           ├── ui/           #   pi-tui components
│           └── agents/       #   LLM role prompts
├── python/ultimate_coders/  # Python layer
│   ├── agent/               #   Worker, Sandbox, Scheduler
│   ├── dashboard/           #   FastAPI metrics + SSE
│   ├── search/              #   SearchQuery builder
│   └── memory/              #   Memory read/write
├── dashboard/               # Vite + React web dashboard
├── docker/                  # Dockerfiles + compose + configs
├── tests/python/            # Python tests
└── vendor/oh-my-pi/         # OMP upstream (submodule)
```

## Build & Run

```bash
cargo check                  # Check all crates
cargo test -p uc-engine      # Run engine tests (no features)
maturin develop              # Build Rust extension + install
cargo run -p uc-grpc-server  # Start gRPC server
./run-omp.sh                 # Start OMP with UC extension (primary)
./run-omp.sh --server        # Also start gRPC server in background
./run-omp.sh --build         # Ensure Python package is built first
```

## Key Types

- `EngineApi` trait (uc-types/src/engine.rs) — unified engine contract
- `EngineError` (uc-types/src/error.rs) — shared error types
- `SearchQuery/SearchResult` (uc-types/src/search.rs) — hybrid search
- `MemoryKey/MemoryEntry` (uc-types/src/memory.rs) — layered memory
- `Task/Subtask/AgentEvent` (uc-types/src/agent.rs) — orchestration

## Repository

- **Remote:** https://github.com/JameryW/UltimateCoders
- **Default branch:** main
