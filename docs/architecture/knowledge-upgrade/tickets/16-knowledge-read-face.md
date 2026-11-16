# 16 — 知识层读面：概览与拒绝理由分布 (T16)

**What to build:** 一个只读聚合面，把权威记录（`attempt_chain` /
`knowledge_evidence` / `knowledge_claims` / `knowledge_pages`）汇总成有界、
log-safe 的概览：attempt 结局、证据种类、claim 状态、**门裁决的允许/拒绝与理由
直方图**、知识页生命周期计数。目标是把「知识库长什么样、为什么拒绝共享」变成
可读事实，供 dashboard / 运维 / A-B 评测消费。

**Blocked by:** T04（补丁链）、T07/T08（证据与门）、T12（集成收据）、T14（读面）
**Spec:** [spec-p0.md](../spec-p0.md)（验收指标里的「误注入 / 未授权提升 = 0」需要可观测证据）
**Status:** implemented（`python/ultimate_coders/knowledge/dashboard.py`；
HTTP `GET /dashboard/api/knowledge`（`python/ultimate_coders/dashboard/app.py`，与其它读面同鉴权、`limit` 1..5000）；
React 面板 `dashboard/src/lib/knowledge.ts` + `components/panels/KnowledgeOverviewPanel.tsx`
（TaskDetail 内挂载，counts-only，7 个 node 测试，`tsc -b` / `eslint` 干净）；
CLI `python -m ultimate_coders.knowledge.dashboard --state ... [--task-id ...] [--json]`；
`tests/python/test_knowledge_dashboard.py` 17 tests，其中 4 个覆盖 HTTP 路由）

**Implementation**

1. `claims.record_gate_decision(ledger, *, claim, decision, target, source)`：把一次
   门裁决写成 `VALIDATION` 证据，payload 只含 `allowed` / `reasons`（≤12 个）/
   `target` / `claim_id` / `strategy`，**不含 claim statement**；证据 id 由该 payload
   派生，所以重放同一次拒绝不会虚增计数。`validate_and_promote_delivery`、
   `promote_failure_claim` 与 `integration.record_integration` 的晋升路径都会记录。
2. `aggregate_knowledge(state, *, task_id="", limit=5000)`：每个命名空间最多扫
   `limit` 行（1..5000），`truncated` 明说是否有截断；`task_id` 按 attempt task id
   过滤证据/claim/补丁链、按 scope graph id 过滤页面；未知值统一进 `unknown`，
   坏行跳过而不抛错。
3. 输出固定 schema `knowledge-overview/1`：`attempts.by_outcome`、
   `evidence.by_kind/by_visibility/verified`、`claims.by_status/by_visibility`、
   `gate.allowed/denied/other/reasons`、`pages.by_lifecycle/locked`、
   `scanned` 与 `stage`。
4. `render_text()` 与 CLI `--json`；`load_state()` 接受 SQLite 路径或
   `postgres://` URL。
5. HTTP 读面 `GET /dashboard/api/knowledge?task_id=&limit=`：`_check_auth` 与
   context manifests / decisions 路由一致（远程需 token、localhost 直通），
   `limit` 非法时 400，其余委托 `aggregate_knowledge`，坏状态返回 503。
6. 前端读面 `dashboard/src/lib/knowledge.ts`（`knowledgeOverviewQuery` /
   `knowledgeStageLine` / `knowledgeCountsLine` / `knowledgeGateLine` /
   `knowledgeOutcomeLine` / `knowledgeEmpty`）与
   `components/panels/KnowledgeOverviewPanel.tsx`（TaskDetail 内挂载）：只读已知
   计数字段，门理由最多列 3 条，`truncated` 在 stage 行标出，空数据显式说明
   「本任务未采集」；不可用时显示 unavailable 而非臆造计数。

**Acceptance gates**

- [x] 空状态输出全零且 schema 稳定；计数口径固定样例断言。
- [x] 门拒绝理由进入直方图（`attempt_not_successful`、`graph_scope_missing`、
      `validation_missing` 各有用例），允许与拒绝分开计数。
- [x] 重放同一拒绝只留一条证据，不虚增拒绝分布。
- [x] 任务过滤隔离证据、claim 与页面；`limit` 截断时 `truncated` 为真。
- [x] 输出不含 claim statement / page body / 签名原文（`SECRET` 字面量断言）。
- [x] CLI 输出合法 JSON，缺 `--state` 时报错退出。
- [x] HTTP 路由注册并通过鉴权/`limit` 校验，返回同一份 schema 且不含内容
      （远程无 token 401、localhost 直通、`limit=0/9999/abc` 400）。
- [x] React 面板消费同一接口：TaskDetail 内渲染 stage / 计数 / 结局 / 门行，
      空数据与不可用状态各有文案，7 个 node 测试，`tsc -b` / `eslint` 干净。

**Rollout:** 纯只读聚合，无开关、不写权威；随时可跑。
**Risks:** 直方图只统计被记录的裁决——旧数据（T16 之前）没有 `VALIDATION` 证据行，
会被记在 `gate.other` 之外的 0，不能反推历史拒绝率；`limit` 是小样本窗口，
`truncated` 为真时不应把计数当全量。
