# Research: Text Index Persistence

- **Query**: 如何持久化 gateway 的 `TextSearchIndex`（内存 inverted_index），使重启不丢失文本搜索能力
- **Scope**: 内部代码库分析（无外部 web 检索）
- **Date**: 2026-07-02

## 现状

`TextSearchIndex`（`crates/uc-engine/src/indexer/text.rs:22-29`）：
- `inverted_index: HashMap<String, Vec<Posting>>` —— token → (repo_id, file_path, line, tf)
- `documents: HashMap<(repo_id, file_path), DocumentMeta>`
- `doc_count: u64`
- 重启即失。仅 AST→Postgres、Semantic→Qdrant 持久。
- 受 `indexing` cargo feature gate 控制；`not(feature)` 下是空 stub。
- 已有方法：`index_file`/`remove_file`/`remove_repo`/`search`。

repo 已有的持久后端：
- **TiKV** raw KV（短期记忆用，`memory:{scope}:{id}:{key}` 编码）
- **Postgres**（AST symbols/references，`metadata/postgres.rs:183-236`，已有 `symbols`/`references` 表）
- **Qdrant**（语义嵌入，1024-dim）
- **`CheckpointManager`**：事件溯源快照模式，注释明说"production would go to TiKV"

## 方案

### Approach A: 从源码重建（rebuild-on-startup）— 推荐 MVP

**How**: gateway 启动时，遍历已注册 repo（Postgres `repos` 表有 path/commit），重新 `index_file` 每个 AST 已知文件到内存。文本索引本身不持久化，靠"从 Postgres symbols 表 + 文件系统源码重建"。

**Pros**:
- **零新代码用于持久化** —— 复用现有 `index_file` + 已有 repo 遍历逻辑。
- 无 schema 变更，无新存储依赖。
- 文本索引本质是源码的纯函数，重建 = 源码 truth，无一致性问题。
- AST symbols 已在 Postgres，遍历 symbols 表比遍历文件系统更快（跳过非代码文件）。

**Cons**:
- 启动延迟 ∝ repo 大小。对中小 repo（< 10k 文件）秒级可接受。
- 每次重启全量重建，无增量快照。

**Files**:
| File | Change |
|------|--------|
| `crates/uc-engine/src/indexer/mod.rs` | `IndexPipeline::load_from_postgres()` 启动钩子，遍历 symbols 表重建 text index |
| `crates/uc-engine/src/metadata/postgres.rs` | 加 `list_all_indexed_files() -> Vec<(repo_id, file_path, language, content_path)>` |

### Approach B: TiKV 快照（snapshot-to-tikv）

**How**: `TextSearchIndex` 加 `snapshot()` / `load_snapshot()`。snapshot 序列化整个 HashMap 到 TiKV 一个 key（或分片到 prefix `text_index:{token}`）。启动时 `load_snapshot()` 重建。

**Pros**:
- 启动快（O(索引大小) 反序列化，无需读源码）。
- 与 `CheckpointManager` 的"would go to TiKV"注释一致。

**Cons**:
- 序列化大 HashMap 的成本；单个大 value 是 TiKV 反模式（应分片）。
- 分片到 per-token key 后，reload 要 scan 全 prefix，与重建成本接近。
- **一致性问题**：快照可能落后于源码；需配合 file-changed 增量更新（本任务索引块要做的事），否则快照=陈旧。
- 新 schema、新代码。

**Files**: `text.rs`（snapshot/load）、`memory/short_term.rs` 或新 `indexer_store.rs`、`mod.rs`。

### Approach C: Postgres 持久化（postings 表）

**How**: 新建 Postgres `text_postings` 表 `(repo_id, file_path, line, token, tf)`，`index_file` 时 upsert，`remove_file` 时 delete。内存索引改为 Postgres-backed 缓存（启动时全量加载到 RAM，运行时双写）。

**Pros**:
- 与 AST 的 Postgres 用法一致，运维统一。
- 增量更新天然支持（行级 upsert/delete）—— 与 file-changed 增量 reindex 协同最好。
- 持久 + 一致。

**Cons**:
- 写入成本：每个文件 N 个 token 行，upsert 量大。可批量。
- schema 变更 + migration。
- 比 Approach A 代码量大。

**Files**: `metadata/postgres.rs`（新表 + CRUD）、`indexer/text.rs`（双写）、`indexer/mod.rs`。

## Recommendation

| Criterion | A (rebuild) | B (TiKV snapshot) | C (Postgres) |
|-----------|-------------|-------------------|--------------|
| 新代码量 | 最少 | 中 | 中多 |
| 启动速度 | 慢（全量重建） | 快 | 中（全量加载） |
| 与 file-changed 增量协同 | 无需（每次重建） | 需额外增量 | 天然协同 |
| 一致性 | 最强（源码 truth） | 弱（快照陈旧） | 强 |
| 新依赖/schema | 无 | TiKV key schema | Postgres 表 |

**对单 gateway + 中小 repo 的 MVP**：**Approach A**（从源码重建）最懒最正确 —— 文本索引是源码的纯函数，重建即真理，零持久化代码。本任务索引块的核心其实是 **file-changed 增量 reindex**（让中心索引跟随 worker 编辑保持新鲜），而非"重启不丢"——重启重建已足够。

如果启动延迟成为问题再升级到 C（Postgres），它同时解决持久化 + 增量更新，但代码量更大。B 不推荐（快照陈旧 + TiKV 大 value 反模式）。

## Caveats

- 无外部 web 检索；结论基于代码库内部分析 + 搜索引擎通用模式（Tantivy/Lucene 的 snapshot-to-disk 是行业标准，但本任务可避免实现它）。
- 未测量重建延迟；对 > 10k 文件的 repo 需实测后决定是否升级到 C。
