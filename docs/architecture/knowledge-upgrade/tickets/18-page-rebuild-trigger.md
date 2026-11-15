# 18 — 页面自动重建触发方 (T18)

**What to build:** 版本化知识页（`knowledge/pages.py`）此前只能手工 `add`/`rebuild`，
没有生产触发方。这一票把「谁触发 rebuild」定下来并接线：**merge arbiter 在
stage `canonical` 且本批确有 REPOSITORY 晋升时，用提升后的 claim 与集成收据的
binding 重建仓库页**；页面仍是读模型投影，**不进 prompt**（prompt 可见知识仍走
claim 检索面 T14）。

**Blocked by:** T04（补丁链）、T07/T08（证据与门）、T12（集成收据）、T14（读面）、
T16（读面聚合页面计数）
**Spec:** [spec-p3.md](../spec-p3.md)、[spec-p0.md](../spec-p0.md)（P4 是投影，不参与 Stage A–E）
**Status:** implemented（`python/ultimate_coders/knowledge/integration.py::_rebuild_pages`；
`knowledge/pages.py::rebuild(..., binding=...)`；`tests/python/test_merge_arbiter_knowledge.py`
新增重建/幂等/失败隔离用例，`tests/python/test_knowledge_pages.py` 新增显式 binding
覆盖用例）

**Implementation**

1. `KnowledgePages.rebuild` 增加 `binding: Any = None`：调用方知道投影锚点（如
   arbiter 已看到 landing）时显式钉住 revision / integration ref，省掉「没有
   deepening 可猜」的伪造；不传时行为不变（deepening head → 既有 revision）。
2. `integration.record_integration` 收集本批晋升成功的 claim；循环结束后调用
   `_rebuild_pages(state, claim_scope, promoted_claims, binding)`：
   - 页面 scope **丢掉 `graph_id`**（canonical 页描述仓库/分支，不描述单个图）；
   - binding 用集成收据的 landed sha / `origin/<base>`，并带 repo_id；
   - 页面正文仍是 `_auto_body` 的计数事实（claims/evidence/revision），不复制
     claim statement 或代码文本。
3. 失败隔离：投影异常只让 summary 的 `pages` 归 0、`errors` +1，**不影响**
   `record_integration` 的返回值语义，更不影响合并/push。
4. summary 新增 `pages` 键（0/1）；stage 未到 canonical 或没有晋升时为 0，读面
   （T16）继续按生命周期聚合页面数量。

**Acceptance gates**

- [x] 仅 canonical + 有晋升时才重建；shadow/capture-only 批次写入 0 页。
- [x] 一个仓库/分支只有一页（scope 不含 graph_id），claim_refs 指向权威 claim。
- [x] 页面 binding 钉住集成收据的 landed revision / integration ref。
- [x] 重复批次幂等：同一批 claim 再跑不新增页面、不膨胀 refs。
- [x] 投影失败被计为 `errors`/`pages=0`，claim 仍是 PROMOTED，合并结果不变。
- [x] 页面正文只有计数事实（无 claim statement / 代码文本）。

**Rollout:** 只随 stage `canonical` 生效（默认关）；无独立开关。
**Risks:** 页面重建发生在 arbiter 进程，若状态库不可用只丢投影、不影响合并；
「页面是否进 prompt、是否同步 Qdrant/TiKV、是否占 Stage 门」已由
[D7](../issues/D7-page-projection-boundary.md) 定案为「都不」——页面保持只读投影，
消费面只有读面（T16）与将来的 planner 视图，避免把投影误当权威或重复注入。
