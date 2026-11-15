# D7 — 知识页投影边界：不自动进 prompt、不同步向量库、不参与放量门

Status: decided
Decided: 2026-11-15

## Question

P4 的三个模块（`survey.py` / `deepen.py` / `pages.py`）已落盘，T18 也把「自动
rebuild」接在 arbiter 的 canonical 晋升之后。剩下的边界问题一直记为「待定」：

1. 版本化知识页要不要像 claim 一样进入 prompt？
2. 页面要不要同步进 Qdrant / TiKV 等检索后端？
3. 页面算不算 Stage A–E 放量门的一部分？

## Decision

**知识页是只读派生投影：不进入 prompt、不同步向量库、不参与 Stage 门；它的
唯一消费面是 Dashboard 读面与将来的 planner 视图，且必须按修订绑定读取。**

1. **权威不变量（D1 的延伸）**：页面的权威仍在 `RuntimeState` 的
   `knowledge_pages` 命名空间；页面 = 已晋升 claim 的聚合视图，可由
   `KnowledgePages.rebuild()` 从 claim/证据确定性重建。删掉整个
   `knowledge_pages` 命名空间不会丢失任何不可重建的事实。
2. **不进 prompt**：worker 的 prompt 知识面只允许两条读路径——
   ① `knowledge/retrieval.py` 的已晋升 claim（`Tier.EVIDENCE`，逐 claim 带
   visibility/verified），② `knowledge/recovery.py` 的本 attempt 链恢复视图。
   页面正文是计数摘要（`_auto_body`），把它注入 prompt 会让同一个事实以
   「聚合视图 + 逐 claim」两种口径重复出现，且绕开逐 claim 的可见性/修订判定。
   **D5 第 4 条中「仓库级知识页进入 prompt」的措辞由本决策收窄为 claim 面。**
3. **不同步向量库**：Qdrant/TiKV 在本仓库是 Memory 检索的投影，不是知识权威。
   把页面写进向量库会制造第二条读路径：它需要自己的一套 stale/supersede/retract
   传播与修订过滤，而 gate 的保证只在 `knowledge_claims` 上成立。若未来要做，
   必须是新决策 + 新票（含重建、撤回传播、修订过滤与 A-B），不在本波次。
4. **不占 Stage**：`UC_KNOWLEDGE_STAGE` 的 A–E 只描述采集/prompt 可见性；
   页面始终是可重建的旁路投影。T18 的触发点只是「canonical 批的副产物」，
   不改变任何 stage 语义。
5. **消费规则**：任何页面消费方（当前是 `knowledge/dashboard.py` 的计数；
   将来的 planner 视图）必须：按 `scope` 隔离、按 `RevisionBinding.appliesTo`
   过滤、只暴露 `to_summary()` 级别的字段（不带 body/diff），并且读失败要显式
   "不可用" 而不是返回空页面冒充「没有知识」。

## Why

- **单一权威**：D1 已规定证据/声明只住 RuntimeState；让页面进第二阶段存储会把
  「哪份知识是真的」重新变成两个答案。
- **不重复注入**：prompt 的预算（D6）是稀缺资源；claim 面已经带门判定的可见性，
  聚合页面没有额外证据价值，只会挤掉真正的证据条目。
- **可回滚**：页面是投影，重建即可；它不是 rollout 风险面，因此也不该让
  Stage 的语义变复杂。
- **诚实**：页面目前只被 Dashboard 计数消费，页面正文尚未被任何运行路径读取；
  与其提前接一条没有验证的读路径，不如把边界写死。

## Boundary（本决策不做什么）

- 不实现页面 → 向量库同步，也不新增第二个页面存储。
- 不实现页面 → prompt 注入；也不把页面正文写进事件/日志（`to_summary()` 已排除 body）。
- 不改变 T18 的触发时机（仅 arbiter canonical 批）、不改 D5/D6 的数字。
- 不承诺未来 planner 视图的具体形态；planner 预算开关见 T17。

## Implementation ticket

T18（页面自动重建与投影边界）、T16（Dashboard 读面计数）。
