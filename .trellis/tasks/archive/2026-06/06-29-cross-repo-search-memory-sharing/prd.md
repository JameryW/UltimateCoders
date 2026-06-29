# PRD: 完善跨仓库代码检索与记忆共享能力

## Problem

当前系统已有混合检索(Text+Semantic+AST)和分层记忆(Task/Project/Global)的基础架构，但存在三个关键缺口：

1. **Worker 端无法跨仓库检索** — Python Worker 执行 subtask 时，搜索只走本地 Engine（无索引数据），未通过 gRPC 调用 Gateway 的 HybridSearchEngine
2. **跨 Worker 记忆不可见** — Worker A 写入的记忆，Worker B 读不到（除非两者都通过 gRPC Gateway 访问共享的 TiKV/Qdrant），Worker 本地 Engine 无索引缓存
3. **无自动仓库发现** — Python SearchQuery.in_repos() 需要手动指定 repo_ids，没有自动获取已索引仓库列表的便捷方法

## Solution

### 1. Worker 搜索走 Gateway gRPC（关键路径）

- Worker 在 `grpc_endpoint` 已配置时，搜索请求走 `Engine.search()` (gRPC mode)
- Worker 在 `grpc_endpoint` 未配置时，走本地 Engine（现有行为不变）
- 修改 `NatsWorker._init_components()` 确保 Worker mode 的 Engine 使用 gRPC

### 2. Worker 记忆共享通过 Gateway

- Worker 的 `write_memory()` / `read_memory()` / `search_memory()` 已有 gRPC fallback 机制
- 确保 Worker mode 的 Engine 也配置 `fallback_mode="auto"` — gRPC 优先，本地兜底
- 添加 `list_repos()` 便捷方法到 Python Engine — Worker 可发现已索引仓库

### 3. 跨仓库检索集成

- `SearchQuery` 添加 `in_all_repos()` 方法 — 自动填入 `list_repos()` 返回的 repo_ids
- Worker 执行 subtask 时，自动注入 project 相关的 repo_ids 到搜索上下文
- 在 `Worker.execute_subtask()` 中提供 `search_across_repos(query)` 便捷方法

## Scope

- **In**: Engine.search gRPC 路径、Worker 搜索集成、list_repos 便捷方法、SearchQuery.in_all_repos()
- **Out**: 不改 Rust 端 HybridSearchEngine 逻辑、不加新存储后端、不改 Qdrant/TiKV schema

## Acceptance Criteria

1. Worker mode + grpc_endpoint 配置时，`engine.search(query)` 走 gRPC 到 Gateway
2. `engine.list_repos()` 返回已索引仓库列表（gRPC 或 local）
3. `SearchQuery("auth").in_all_repos(engine)` 自动填入所有已索引 repo_ids
4. Worker subtask 执行时可调用 `search_across_repos(query)` 跨仓库检索
5. 现有测试不回归

## Technical Notes

- Engine.__init__ 已有 mode="grpc" + fallback_mode="auto" 机制
- NatsWorker._init_components() 已创建 Engine(mode="local") — 需改为根据 grpc_endpoint 选择 mode
- GrpcEngineClient 已实现 search()、list_repos() — Python Engine.list_repos() 需加 fallback 包装
- Worker.execute_subtask() 中 engine 已可访问 — 需加搜索便捷方法
