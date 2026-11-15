# D1 — 知识层的存储权威：不建第二套权威库

Status: decided
Decided: 2026-11-15

## Question

V2.0 知识升级把 attempt 补丁链、证据、声明（claim）、知识页与检索视图拆成六模块。
Qdrant（长期记忆 + 语义检索）与 TiKV（短期记忆）已经承载「记忆」数据，那么证据与
声明应该新建一套权威存储（新 DB、新 Rust Knowledge Authority 服务），还是复用现有的
记录基座？投影（Qdrant / TiKV / 未来知识页）与权威之间谁是唯一真相？

## Decision

**MVP 不新增权威存储、不新增 Rust Knowledge Authority 服务；证据、声明与 attempt
链以 PostgreSQL（开发/测试为 SQLite）承载的 `RuntimeState` 为唯一权威，Qdrant /
TiKV / 知识页 / 检索视图一律是可重建投影。** Python 侧 facade 保持「可上移 Rust」的
形状：纯数据契约集中在 `knowledge/model.py`，不依赖运行时。

1. **权威**：`knowledge/evidence.py` 的 `knowledge_evidence` / `knowledge_claims`
   命名空间、`knowledge/attempt_chain.py` 的 `attempt_chain` 命名空间，全部经
   `RuntimeState.mutate` 落库；与 `decision_record`、`context_manifest`、
   `tool_effects` 同一套 CAS / 版本语义。
2. **投影**：Qdrant 向量、TiKV 短期记忆、未来版本化知识页（Pages）都只从权威记录
   派生；允许删库重建，**不得反向写回**权威。
3. **不新增服务**：不建 Rust Knowledge Authority、不在 Gateway 增加知识 gRPC 面。
   `knowledge` 包除 `model` / `rollout` 外都在 worker 侧，且
   `knowledge/__init__.py` 明示 never imported by Rust facade。
4. **词汇与存储分离**：`knowledge/model.py` 保持 import-light（`AttemptRef`、
   `Visibility`、`EvidenceKind`、`ClaimStatus`、`ValidationResult`、
   `RevisionBinding`、`KnowledgeScope` 与 `canonical_json`/`digest`/`safe_text`
   工具）。将来若要把权威上移到 Rust，搬的是存储实现，不是语义。

## Why

- **一致性优先于吞吐**：知识事实要和 Decision Record、context manifest、effect
  ledger 共用同一套记录基座，否则恢复时会出现两套状态各说各话——这正是
  `docs/agent-os-architecture.md` §2「每个事实只有一个所有者」要避免的反例。
- **没有规模证据要求新库**：MVP 只服务 attempt 链与有界 Recovery View，量级是每个
  attempt 几 KB 的日志安全 JSON，`RuntimeState` 足够。
- **第二套权威的代价已经付过一次**：V8 波次的 `CONTRACT_VERSION` lockstep 与升级
  顺序（先 Gateway 后 Worker）是跨语言契约的成本；知识层没有第二个契约版本可付。

## Boundary（本决策不做什么）

- 不判断向量索引是否够新：检索质量归检索层，不是权威问题。
- 不选知识页 Pages 的存储位置（P4/P5 阶段决定；本决策只钉死它的「投影」身份）。
- 不把 Decision Record / effect ledger 合并进知识层——它们各有权威，互不复用。
- 不排期 Rust facade 上移；只保证形状可搬。

## Implementation ticket

T01（`knowledge` 包骨架 + `model.py` 冻结词汇）、T04–T06（`attempt_chain` 命名空间与补丁采集）、
T07–T09（`knowledge_evidence` / `knowledge_claims` 命名空间与采集接线）。
