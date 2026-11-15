# 14 — 图共享读取面与按预算检索 (T14)

**What to build:** 让 stage `graph` 的写入真正被读到：worker 在组装 attempt
上下文时，从**本任务图**读取门已晋升的 claim，按视图预算打包为
`Tier.EVIDENCE` 的 `ContextItem`，并把「用了几条 / 剩几条 / 花了多少 token」
写进 `subtask_started.data["knowledge"]`（只有计数与预算，无 claim 正文）。

**Blocked by:** T07（账本）、T08（门读取规则）、T10（编译器优先级）
**Spec:** [spec-p3.md](../spec-p3.md)、[spec-p0.md](../spec-p0.md)（P5 切片）
**Status:** implemented（`python/ultimate_coders/knowledge/retrieval.py`；
`rollout.retrieval_budget_tokens()` / `UC_KNOWLEDGE_RETRIEVAL_BUDGET_TOKENS` 默认
500（clamp 128–4000）；worker `_knowledge_context_items`；
`tests/python/test_knowledge_retrieval.py` 10 tests +
`tests/python/test_worker_attempt_evidence.py` 注入用例）

**Implementation**

1. `retrieve_claims(ledger, *, scope, ...)` 在 `rollout.graph_sharing_enabled(ledger.stage)`
   为假时返回 `[]`——读面不早于 stage `graph`，与写入门一致。
2. 读取复用 `EvidenceLedger.visible()`（作用域隔离 + 门读取规则），默认
   `reader_visibility=GRAPH, min_visibility=GRAPH`；排序按可见性、置信度、claim_id。
3. `claims_to_context_items(claims, *, budget_tokens, source="knowledge-claims")`
   打包 EVIDENCE 项：`item_id="knowledge-claim:<id>"`、内容
   `Claim (<visibility>): <statement>`（statement ≤400）、`trust_class="knowledge_claim"`、
   `verified=status is promoted`、`relevance=confidence`、`token_cost=estimate_tokens`；
   超预算**跳过整条**，从不截断半条 claim。
4. worker：`subtask.parent_id` 为空时不读（无图作用域）；stage <`graph` 时整段跳过，
   prompt 与无知识层时逐字节一致；异常只 `logger.debug`。
5. 事件摘要 key 固定：`claims_available / claims_used / tokens / budget_tokens /
   truncated / stage / claims_visible`。

**Acceptance gates**

- [x] stage <`graph` 时不读（即使库里已有 GRAPH claim）；stage `graph` 才注入。
- [x] ATTEMPT 级 claim 永不可见；跨图作用域不可见；修订不匹配的读者被过滤。
- [x] 预算内的 claim 以 EVIDENCE 层进入编译器；超预算整条跳过并 `truncated=True`。
- [x] `subtask_started.data["knowledge"]` 只含计数/预算，不含 claim 正文。
- [x] 读失败不影响子任务（best-effort）。

**Rollout:** 读面随 `UC_KNOWLEDGE_STAGE`≥`graph`；默认关，stage C 及以下行为不变。
**Risks:** claim 语句本身由 worker 生成，若措辞含误导性断言，读取面会放大它——
这是「晋升由门裁决」之后的残余风险，靠校验策略而非读取面解决。
