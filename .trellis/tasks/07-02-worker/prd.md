# 所有 worker 共享分布式记忆与分布式代码库索引

## Goal

让分布式部署的多 worker 真正共享**记忆**和**代码库索引**，消除当前两处实质断点：
gRPC fallback 期间本地写入不回灌、worker 实时编辑不反映到中心索引。使任意 worker
的写入/编辑对其余 worker 可见，且中心索引对在飞编辑保持新鲜。

## What I already know

来自代码勘察（crates/uc-engine, python/ultimate_coders）：

**记忆块现状（已集中，有断点）**
- worker 的 `read/write/delete/search_memory` 全走 gRPC 到 gateway 单 `LocalEngine`，
  后端 TiKV（短期 `memory:{scope}:{scope_id}:{key}`）+ Qdrant（长期 1024-dim）。
  `crates/uc-engine/src/memory/{mod,short_term,long_term}.rs`
- gRPC server 是薄 pass-through：`crates/uc-grpc/src/server.rs:2558-2650`
- 跨 worker 失效已有：写/删时 NATS 广播 `uc.memory.changed`，其余 worker 清搜索缓存。
  `python/ultimate_coders/agent/worker.py:1199-1222`, `nats_worker.py:1728-1760`
- **断点 1**：`Engine._try_grpc_with_fallback` 在 gRPC 故障时切到本地 in-process
  `LocalEngine`（`engine.py:222-265`）。fallback 期间的写入落进**孤立进程内** TiKV/Qdrant
  fallback（`new_fallback` 的 Vec/HashMap），恢复 gRPC 后**不回灌**。

**索引块现状（已集中，有断点）**
- gateway `IndexPipeline` = Text（内存 inverted_index, `indexer/text.rs:24`）+ AST
  tree-sitter→Postgres（`indexer/ast.rs`, `metadata/postgres.rs:183-236`）+ Semantic
  →Qdrant（`indexer/semantic.rs`, `search/semantic.rs`）。`HybridSearchEngine` 合并三者。
- `SearchQuery.in_all_repos()` 跨仓库搜索已工作（`search/query.py:29-47`，
  `worker.py:1086-1110`），且自动注入 subtask prompt（`worker.py:1046-1084`）。
- **断点 2**：文本索引纯内存，gateway 重启即失（仅 AST/Semantic 持久）。
- **断点 3**：worker 编辑文件后广播 `uc.file.changed`（`worker.py:981-1010`），
  但 gateway 侧无订阅、不增量 reindex → 中心索引相对 worker 在飞编辑是陈旧的。
- **断点 4**：codegraph 是每 worker 独立 SQLite（`.codegraph/codegraph.db`），无统一视图。

## Assumptions (temporary)

- 选择「全分布式索引增强」+「带版本/时间戳 CAS 回灌」（用户已定）
- TiKV/Qdrant/Postgres 已作为 gateway 的共享存储，多 worker 共用同一 gateway 实例
- worker 与 gateway 时钟有合理同步（CAS 时间戳用 monotonic+wall 混合或 HLC）

## Open Questions

全部锁定（见 Decision）。后续如需 HLC/Postgres 持久化升级，按升级路径走。

## Research References

- [`research/cas-replay.md`](research/cas-replay.md) — TiKV 有原生 CAS；单 gateway MVP 用 app 级版本 + in-process lock + blind put（Approach B），wall-clock LWW，留 HLC 升级路径
- [`research/text-index-persistence.md`](research/text-index-persistence.md) — 文本索引是源码纯函数；MVP 用启动重建（Approach A），零持久化代码；启动慢再升级 Postgres（C）

## Decision (ADR-lite)

**记忆 CAS**：Approach B（app 级版本 + in-process lock + blind put, wall-clock LWW）。
- `MemoryEntry`/`StoredEntry` 加 `version: u64`（= wall-clock ms）。
- gateway `MemoryStore` 加 per-key `DashMap<MemoryKey, Mutex<()>>` 串行化 read-compare-write。
- Python `Engine` 加 `_fallback_write_log` WAL，`_check_grpc_recovery` 成功后 drain 调新 `ReplayMemoryWrite` RPC。
- 预留 HLC 升级：version 字段语义是"全序时间戳"，未来换成 HLC tuple 不破坏接口。

**文本索引持久化**：Approach A（启动从 Postgres symbols + 源码重建）。
- `IndexPipeline` 启动钩子遍历已索引文件重建 text index。零持久化 schema。

**file-changed 增量 reindex**：gateway 订阅 NATS `uc.file.changed`，对变更文件 `index_file`（全文件重索引，非符号 diff——最懒正确）。

**codegraph 统一**：~~不废弃 per-worker SQLite，但 uc-lsp 的 codegraph fallback 增加一条"先查 gateway Postgres symbols，miss 再查本地 SQLite"的路径。统一视图=Postgres 优先。~~

**降级为 Out of Scope**（见下）。理由：uc-lsp 已优先用真 LSP，uc-engine MCP 已暴露 gateway AST search（跨 worker 符号可见性已通过 gateway search 实现），codegraph 是 fallback 的 fallback；让 stdio MCP server 持有 gRPC engine 句柄 + 结果格式映射成本高、价值边缘。前 3 块已实质满足"分布式代码库索引"目标。

## Out of Scope

- HLC 实现（升级路径，本任务不做）
- TiKV CAS mode / `with_atomic_for_cas()`（B 不需要）
- 文本索引 Postgres 持久化（C 升级路径，本任务不做）
- 长期记忆（Qdrant）fallback 回灌（短期 TiKV 优先，长期另开任务）
- 跨 host 真分布式部署
- uc-lsp 改为共享
- **codegraph→Postgres 统一视图**（降级：uc-lsp 的 codegraph fallback 不接 gateway；跨 worker 符号查询走 uc-engine MCP 的 gateway AST search，已满足）

## Requirements (evolving)

### 记忆块
- gRPC fallback 期间本地写入带版本/时间戳记录
- 恢复 gRPC 后按时间戳 CAS 回灌到 gateway，冲突时取较新版本
- 回灌后正常广播 `uc.memory.changed`

### 索引块
- 文本索引持久化，gateway 重启后可重建（从存储加载，不必全量 reindex）
- gateway 订阅 `uc.file.changed`，对变更文件增量 reindex
- worker 编辑在合理延迟内（秒级）反映到中心索引
- codegraph 知识与中心 Postgres 索引有统一视图

## Acceptance Criteria (evolving)

- [x] worker A 在 gRPC fallback 期间写记忆，恢复后 worker B 能读到（端到端验证 + `test_replay_write_applies_newer_version`）
- [x] worker A 改文件 X，worker B 在秒内 search 能命中 X 的新内容（`test_reindex_file_content_reflects_edit` + gateway `uc.file.changed` 订阅）
- [x] gateway 重启后 text search 仍可用（`test_restore_text_index_after_restart`，从源码重建）
- [x] CAS 回灌冲突有日志/指标可观测（`replay_write skipped stale` 日志 + `MemoryReplayResult.applied` 字段）

## Definition of Done (team quality bar)

- Tests added/updated（unit + 集成：双 worker 场景）
- cargo check + cargo test -p uc-engine + python tests 绿
- CI 绿
- 行为变更在 CLAUDE.md / 相关文档更新

## Out of Scope (explicit)

- 跨 host 真分布式部署（docker swarm / 远程 docker context）—— 数据层已 cross-host-safe
- 替换 TiKV/Qdrant/Postgres 存储后端
- uc-lsp 改为共享（其本质是 per-worker 本地文件系统操作，不在本任务）

## Research References

- 待补：CAS 回灌/HLC 模式、文本索引持久化方案

## Technical Notes

- 关键入口见上「What I already know」的 file:line
- 用户决策：全分布式索引增强 + 带版本/时间戳 CAS 回灌
