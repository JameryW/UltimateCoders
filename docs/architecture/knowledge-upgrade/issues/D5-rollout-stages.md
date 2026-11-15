# D5 — 分阶段 rollout A–E：默认全关，Stage C 以下 prompt 不变

Status: decided
Decided: 2026-11-15

## Question

知识升级同时改三件事：采集（补丁链 / 证据）、prompt（恢复视图）、可见性
（graph 共享 / 仓库级知识页）。一次性开关会让「行为变了但没人知道是哪一层变的」，
也会让回滚变成猜谜。如何上线、如何回滚、默认状态是什么？

## Decision

**五级递增 rollout stage，默认 A（关闭）；每一级只解锁一个能力面；Stage C
（Recovery）以下 prompt 逐字不变。**

1. **单一开关**：`UC_KNOWLEDGE_STAGE`，由
   `knowledge/rollout.py::stage_from_env()` 解析，取值
   `A`/`off`/`disabled`/空 → `KnowledgeStage.DISABLED`(0)；
   `B`/`shadow`(1)；`C`/`recovery`/`canary`(2)；`D`/`graph`(3)；
   `E`/`canonical`(4)。也接受 `0`–`4`。
2. **fail-closed**：未知值、非法整数一律落到 `DISABLED`（`resolve_stage()`），
   宁可不采集也不进入半开状态。
3. **能力面派生**：`capture_enabled`（≥SHADOW）、`recovery_enabled`（≥RECOVERY）、
   `graph_sharing_enabled`（≥GRAPH）、`canonical_enabled`（≥CANONICAL）、
   `describe_stage()` 是唯一判据，调用点不得各写一份版本比较。
4. **prompt 不变量**：A/B 只写审计与证据（`context_manifest`、`attempt_chain`、
   `knowledge_evidence`），**prompt 逐字不变**；C 起才把有界 Recovery View 注入
   attempt 2+；D/E 才允许共享 claim / 仓库级知识页进入 prompt。
5. **同批执行面开关**（同属本决策，因为它们是「阶段化的资源上限」）：
   `UC_ATTEMPT_CHAIN_MAX_ROUNDS`（默认 `DEFAULT_ATTEMPT_CHAIN_MAX_ROUNDS = 20`）、
   `UC_KNOWLEDGE_MAX_PATCH_FILES`（默认 20）、`UC_KNOWLEDGE_MAX_DIFF_CHARS`
   （默认 20_000）；超限截断并置 `truncated=True`。
6. **默认值只住一个地方**：全部在 `knowledge/rollout.py`；文档只引用常量名，不再
   抄一份数字。

## Why

- **可归因**：`describe_stage()` 的 stage 名会进摘要/日志，排障时「行为为什么变了」
  的第一问有答案。
- **可回滚**：回滚 = 改一个环境变量回 A，不需要发版；已经写入的证据保留（审计面
  不改运行行为）。
- **先把度量跑出来再动 prompt**：用户计划的 MVP 闭环要求「先测出更少重复失败/更少
  重试」，B 阶段就是只测不动的观察窗。

## Boundary（本决策不做什么）

- 不做按租户/按任务的灰度（stage 是进程级 env；多租户分桶留给 P4/P5 的 Gateway
  侧视图预算）。
- 不做 stage 的热更新（改 env 需要重启 worker；与 `CONTRACT_VERSION` 无关系）。
- 不承诺 stage D/E 的行为细节（P4/P5 前沿阶段才决定）。
- 不改变既有 `UC_CONTEXT_BUDGET_TOKENS` 的语义（那是编译预算，见 D6）。

## Implementation ticket

T02（`knowledge/rollout.py` 分阶段开关，fail-closed）。
