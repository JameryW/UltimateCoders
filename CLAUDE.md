# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**UltimateCoders** — 分布式 AI Coding 系统，支持多个 coding agent 以 Orchestrator-Worker 模式协同工作，共享分层 Memory，并集成多 Git 仓库的混合检索能力（Text + Semantic + AST）。

## Architecture

- **Rust 核心引擎** (5 crates): uc-types, uc-engine, uc-grpc, uc-grpc-server, uc-python
- **Python Agent 层**: Orchestrator + Worker (LLM 交互)
- **桥接**: PyO3 FFI (本地) + gRPC (分布式), 运行时切换
- **存储**: TiKV (短期 Memory) + Qdrant (长期 Memory + 语义检索) + PostgreSQL (结构化元数据)
- **通信**: gRPC 同步 + NATS JetStream 异步

## Repository Structure

```
ultimate-coders/
├── Cargo.toml              # Workspace root
├── pyproject.toml           # Maturin build config
├── crates/
│   ├── uc-types/            # Shared types + EngineApi trait
│   ├── uc-engine/           # Core engine (LocalEngine implementation)
│   ├── uc-grpc/             # gRPC server/client + proto
│   ├── uc-grpc-server/      # Standalone gRPC server binary
│   └── uc-python/           # PyO3 Python binding
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
```

## Key Types

- `EngineApi` trait (uc-types/src/engine.rs) — unified contract for all engine operations
- `EngineError` enum (uc-types/src/error.rs) — shared error types, mapped to both PyO3 exceptions and gRPC Status codes
- `SearchQuery/SearchResult` (uc-types/src/search.rs) — hybrid search types
- `MemoryKey/MemoryEntry` (uc-types/src/memory.rs) — layered memory types
- `Task/Subtask/AgentEvent` (uc-types/src/agent.rs) — orchestration types

## Development Progress

- ✅ PR1: Rust workspace + uc-types + uc-engine skeleton
- ✅ PR2: 存储客户端集成 + Memory 读写 (in-memory fallback; TiKV/Qdrant/PostgreSQL clients coded, need infra)
- ✅ PR3: 文本检索 + AST 索引引擎 (language-aware tokenization, tree-sitter AST, text search)
- ✅ PR4: 语义检索 + 混合检索 API (BLAKE3 fallback embeddings, hybrid search engine)
- ✅ PR5: gRPC + PyO3 桥接层 (tonic server/client, proto compilation, PyEngine wired)
- ✅ PR6: Python Agent 层 (Orchestrator + Worker, LLM tool-calling, memory wrappers)
- 🔲 PR7: 容错机制 (Event Sourcing, conflict resolution, LLM rate limiting)
- 🔲 PR8: Docker Compose + CI + 文档

## Repository

- **Remote:** https://github.com/JameryW/UltimateCoders
- **Default branch:** main