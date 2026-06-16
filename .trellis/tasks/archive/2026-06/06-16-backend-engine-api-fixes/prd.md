# Backend EngineApi Fixes

## Goal

修复 Rust 后端 `uc-engine` 中 5 个已确认的 EngineApi 层级缺陷，使其从"骨架可编译"提升到"功能可依赖"。

## What I already know

- `search_memory()` 用零向量查询（`memory/mod.rs:161`），语义搜索永远返回空
- `get_index_state()` 硬编码 files/symbols/chunks count 为 0（`local.rs:449-452`）
- `MemoryStore.search()` 有 `search_with_embedding()` 替代方法，但 `EngineApi` trait 和 gRPC/PyO3 桥接层只暴露 `search_memory(query_text)` 签名，无法传 embedding
- `EmbeddingService` 已实现 BLAKE3 fallback embedding 和 Voyage Code 3 API 两种模式
- `IndexPipeline` 内部在索引时已经计算了 files/symbols/chunks 计数，但 `IndexState` 结构体没有这些字段
- 所有存储测试只跑 fallback 路径（TiKV/Qdrant/PostgreSQL 零集成测试）

## Decisions

### D1: search_memory embedding 策略 → Rust 端内部调 EmbeddingService

- `MemoryStore` 持有 `EmbeddingService` 实例，`search()` 内部将 query text 转为 BLAKE3/Voyage embedding，再调已有的 `search_with_embedding()`
- 零 API 变更，Python/gRPC 调用方无感知
- BLAKE3 零外部依赖可用

### D2: get_index_state 计数来源 → 写入 IndexState

- 索引完成时 `IndexPipeline` 将 files/symbols/chunks 计数写入 `MetadataStore` 的 `IndexState`
- `local.rs` 查询时从 `MetadataStore.get_index_state()` 读取
- 需要给 `IndexState` 加 3 个字段，给 PostgreSQL 表加 3 列（有 migration）
- 查询时零开销

## Requirements

### AC1: search_memory 返回有效结果

- `search_memory(query_text)` 不再使用零向量
- Rust 端内部调用 `EmbeddingService` 将 query text 转为 embedding，再调 `search_with_embedding()`
- BLAKE3 fallback embedding 保证无外部依赖时仍可工作
- Voyage Code 3 API 配置时使用真实语义 embedding

### AC2: get_index_state 返回真实计数

- `files_count`, `symbols_count`, `chunks_count` 从硬编码 0 改为真实值
- 计数来源：`IndexPipeline` 索引完成后写入 `MetadataStore`，查询时从 `MetadataStore` 读取
- `IndexState` struct 新增 `files_count: u64`, `symbols_count: u64`, `chunks_count: u64`
- PostgreSQL `index_state` 表新增 3 列（带 migration）
- Fallback 模式同样支持计数

### AC3: MemoryStore.read 的 include_semantic 语义查找

- 当 `include_semantic=true` 且 short-term 未命中时，使用 BLAKE3 embedding 做 long-term 语义查找
- 替代当前的 "return None" 空实现（`memory/mod.rs:77-78`）

### AC4: 添加单元测试

- `search_memory()` 使用 BLAKE3 embedding 能返回结果
- `get_index_state()` 返回非零计数
- `read()` with `include_semantic=true` 能找到 long-term 中的条目

### AC5: 改进错误处理

- `EmbeddingService` 不可用时（如 Voyage API key 未配 + BLAKE3 编译问题），`search_memory()` 返回空结果 + warning log，而不是 panic 或返回错误

## Acceptance Criteria

- [ ] `cargo test -p uc-engine` 全部通过
- [ ] `search_memory("any query")` 在 BLAKE3 模式下返回相关结果（非空）
- [ ] `get_index_state("indexed-repo")` 返回的 files_count > 0（当 repo 已索引时）
- [ ] `read()` with `include_semantic=true` 能命中 long-term memory
- [ ] 无新 `unimplemented!()` / `todo!()`
- [ ] `cargo clippy` 无新 warning
- [ ] `cargo fmt --check` 通过

## Definition of Done

- Tests added/updated（单元测试覆盖 AC1-AC4）
- Lint / typecheck / CI green（`cargo clippy`, `cargo fmt --check`）
- Docs/notes updated if behavior changes
- Backward compatible — 不破坏现有 API 签名

## Out of Scope

- Docker Compose 基础设施集成测试（TiKV/Qdrant/PostgreSQL 真实连接测试）
- TaskService 持久化（从 HashMap 升级 PostgreSQL）
- AI 任务分解（接入 LLM）
- WatchTask 事件 task_id 关联
- Proto 文件变更
- NATS 消息分发

## Implementation Plan

### PR1: AC1+AC3+AC5 — search_memory embedding 修复
1. `MemoryStore` 新增 `embedding_service: Arc<EmbeddingService>` 字段
2. `MemoryStore::search()` 内部调 `embedding_service.embed(query)` 生成向量，再调 `search_with_embedding()`
3. `MemoryStore::read()` 的 `include_semantic` 分支用 embedding 做 semantic lookup
4. `LocalEngine::new()` 构造时传入 `EmbeddingService`
5. 错误处理：embedding 失败时返回空结果 + warning log
6. 测试：`search_memory()` BLAKE3 模式返回结果；`read()` semantic lookup 命中

### PR2: AC2 — get_index_state 真实计数
1. `IndexState` 新增 `files_count`, `symbols_count`, `chunks_count` 字段
2. `IndexPipeline` 索引完成时将计数写入 `MetadataStore`
3. `local.rs` 从 `IndexState` 读取计数（替代硬编码 0）
4. PostgreSQL migration: `index_state` 表新增 3 列
5. Fallback `IndexState` 同步更新
6. 测试：索引后 `get_index_state()` 返回非零计数

### PR3: AC4 — 测试补充 + clippy/fmt
1. 补充 AC1-AC3 的单元测试
2. `cargo clippy` + `cargo fmt --check` 修复
3. 文档更新

## Technical Notes

- 关键文件：`crates/uc-engine/src/memory/mod.rs`, `crates/uc-engine/src/local.rs`, `crates/uc-engine/src/indexer/mod.rs`
- `EmbeddingService` 在 `crates/uc-engine/src/indexer/semantic.rs`
- `IndexState` 定义在 `crates/uc-engine/src/metadata/postgres.rs`
- `VECTOR_SIZE = 128`（`crates/uc-engine/src/memory/long_term.rs`）
- BLAKE3 embedding: 对 query text 哈希后映射到 128 维 float 向量
- 现有 `search_with_embedding()` 方法可用，不需要重写 long-term 搜索逻辑
