# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**UltimateCoders** — 分布式 AI Coding 系统，以 OMP Extension 为编排核心，Rust Engine 为后端，Python Worker/Sandbox 为 gRPC fallback 路径，共享分层 Memory，并集成多 Git 仓库的混合检索能力（Text + Semantic + AST）。

## Architecture

- **Rust 核心引擎** (5 crates): uc-types, uc-engine, uc-grpc, uc-grpc-server, uc-python
- **OMP Extension** (TypeScript): UC Orchestrator — 任务编排 + UI 组件 + LLM tools + Coding Agent
- **Python Worker/Sandbox**: gRPC LocalWorkerBridge/NATS fallback 路径 (Worker, SandboxManager, LLMClient)
- **桥接**: PyO3 FFI (本地) + gRPC-Web (OMP→Rust), 运行时切换
- **存储**: TiKV (短期 Memory) + Qdrant (长期 Memory + 语义检索) + PostgreSQL (结构化元数据)

## Repository Structure

```
ultimate-coders/
├── Cargo.toml              # Workspace root
├── pyproject.toml           # Maturin build config
├── run-omp.sh               # Start OMP with UC extension
├── crates/
│   ├── uc-types/            # Shared types + EngineApi trait
│   ├── uc-engine/           # Core engine (LocalEngine implementation)
│   ├── uc-grpc/             # gRPC server/client + proto
│   ├── uc-grpc-server/      # Standalone gRPC server binary
│   └── uc-python/           # PyO3 Python binding
├── packages/
│   └── uc-orchestrator/     # OMP extension + rich TUI components
│       ├── src/extension.ts  # Extension entry (commands, shortcuts, renderers)
│       ├── src/orchestrator/ # Core orchestration logic + events
│       ├── src/ui/           # pi-tui components (progress, overlays, formatters)
│       └── src/uc-rpc-server.ts # JSONL stdio bridge for Python
├── python/
│   └── ultimate_coders/     # Python ergonomic layer
├── proto/                   # Shared proto definitions
├── tests/                   # Test suites
└── docs/                    # Documentation
```

## Build & Run

```bash
# Rust build
cargo check                  # Check all crates
cargo test -p uc-engine      # Run engine tests (no features)
cargo test                   # Run all tests (requires storage infra)

# Python build (requires maturin)
maturin develop              # Build Rust extension + install in editable mode

# gRPC server
cargo run -p uc-grpc-server  # Start standalone gRPC server

# UC Orchestrator (OMP extension)
./run-omp.sh                 # Start OMP with UC extension (primary interface)
```

## Key Types

- `EngineApi` trait (uc-types/src/engine.rs) — unified contract for all engine operations
- `EngineError` enum (uc-types/src/error.rs) — shared error types, mapped to both PyO3 exceptions and gRPC Status codes
- `SearchQuery/SearchResult` (uc-types/src/search.rs) — hybrid search types
- `MemoryKey/MemoryEntry` (uc-types/src/memory.rs) — layered memory types
- `Task/Subtask/AgentEvent` (uc-types/src/agent.rs) — orchestration types

## Repository

- **Remote:** https://github.com/JameryW/UltimateCoders
- **Default branch:** main