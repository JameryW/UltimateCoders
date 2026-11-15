# D4 — 证据、声明、校验是三种记录；晋升由门裁决且拒绝理由词表固定

Status: decided
Decided: 2026-11-15
Spec: [spec-p2.md](../spec-p2.md)

## Question

一次 attempt 里既有客观事实（补丁、测试结果、oracle 判定），也有解释（根因、
假设），还有「我们相信什么」（claim）。如果它们混在一个可写记录里，就分不清
「因为什么相信」和「谁说的」。当一条结论要从本次 attempt 升级为图级 / 仓库级
共享知识时，系统凭什么允许、凭什么拒绝？

## Decision

**三类记录分离存储、各自有确定性身份；校验只推进 `VALIDATED`；是否可见 / 是否
晋升由 `EpistemicGate` 裁决；拒绝理由用固定词表，禁止改写。**

1. **证据 `EvidenceRecord`（事实层，不可变）**：`kind`（`EvidenceKind`）、
   `attempt`、`scope`、`visibility`、`revision`（`RevisionBinding`）、有界
   `payload`、`summary`、`trust_class`、`verified`。`evidence_id` 由
   `short_digest(kind, attempt.key(), revision, payload)` 确定性生成，重放幂等。
   信任类与 `verified` 默认由 `model.DEFAULT_EVIDENCE_TRUST` /
   `VERIFIED_EVIDENCE_KINDS` 派生，调用方不得自行宣称「已验证」。
2. **校验 `ValidationRecord`（判定层，append-only）**：`validation_id`、
   `claim_id`、`result`（`PASS` / `FAIL` / `INCONCLUSIVE`）、被校验的 `revision`、
   `evidence_ids`、`strategy`、`validator`、`notes`。每个 claim 最多保留
   `MAX_VALIDATIONS = 20` 条（`evidence.py`，超出丢最旧，写入用 `CAS_RETRIES = 3`
   次版本重试）。
3. **声明 `ClaimRecord`（信念层）**：`claim_id` 由「scope + 归一化 statement」
   确定性生成，**只写一次**；状态机 `CANDIDATE → VALIDATED → PROMOTED`，
   旁路 `SUPERSEDED` / `RETRACTED`（终态，不再可提升）；`human_locked` 保护
   人工结论不被自动覆盖；`superseded_by` / `retract_reason` 记录原因。
4. **校验 ≠ 发布（原则 B）**：`ValidationResult.PASS` 只把 claim 推到
   `VALIDATED`。进入 `GRAPH` / `REPOSITORY` 必须过 `evaluate_promotion()`：
   - 目标可见性 rank 必须**严格更高**（`REASON_TARGET_NOT_HIGHER`）；
   - 终态 claim 不得再晋升（`REASON_CLAIM_TERMINAL`）；
   - `ATTEMPT → GRAPH`：attempt 成功（`REASON_ATTEMPT_NOT_SUCCESSFUL`）、
     未过期（`REASON_ATTEMPT_STALE`）、校验 `PASS`（`REASON_VALIDATION_MISSING`
     / `_FAILED` / `_INCONCLUSIVE`）、校验修订与目标修订一致
     （`REASON_REVISION_MISMATCH`）、scope 有 `graph_id`
     （`REASON_GRAPH_SCOPE_MISSING`）；
   - `GRAPH → REPOSITORY`：额外要求集成收据
     （`RevisionBinding.has_integration`，否则 `REASON_INTEGRATION_MISSING`）
     且推送状态已知（`push_status != "UNKNOWN"`，否则 `REASON_PUSH_UNKNOWN`）。
5. **读也有门**：`EpistemicGate.can_read()`：`ATTEMPT` 仅同一重试链
   （`same_chain=True`，否则 `REASON_ATTEMPT_CHAIN_REQUIRED`）；`GRAPH` 需
   `graph_id` 与 tenant/repo/branch 同时匹配（`REASON_GRAPH_NOT_SHARED`）；
   `REPOSITORY` 需 `KnowledgeScope.allows_read()` 通过且
   `RevisionBinding.applies_to(reader_revision)`（`REASON_CROSS_SCOPE_DENIED` /
   `REASON_REVISION_MISMATCH`）。空 scope 字段是通配，空 reader 字段**不**放宽。
6. **拒绝理由词表固定**：`REASON_*` 共 15 个（`target_not_higher`、
   `attempt_not_successful`、`attempt_stale`、`validation_missing`、
   `validation_failed`、`validation_inconclusive`、`revision_mismatch`、
   `graph_scope_missing`、`integration_missing`、`push_unknown`、`claim_terminal`、
   `cross_scope_denied`、`graph_not_shared`、`attempt_chain_required`、
   `stage_disabled`）。它们是日志与指标键（「stale 误注入 = 0」「未授权提升 = 0」
   按理由聚合），**不得为了文案改名字**；新增拒绝条件只能新增常量。
7. **fail-closed**：未知 stage → `REASON_STAGE_DISABLED`；未知枚举值在
   `model.normalize_*` 处抛 `ValueError`；`can_read` 的默认姿态是拒绝。
8. **日志安全**：evidence payload 与 claim statement 都过 `safe_text()`（statement
   上限 `MAX_CLAIM_STATEMENT_CHARS = 2_000`，summary 上限
   `MAX_SUMMARY_CHARS = 500`）；payload 有界（`MAX_PAYLOAD_CHARS = 8_000` /
   `MAX_PAYLOAD_DEPTH = 3` / `MAX_PAYLOAD_ITEMS = 50`）且对
   `_REDACTED_PAYLOAD_KEYS = {"prompt", "system_prompt", "messages",
   "chat_history", "transcript", "diff", "patch"}` 直接折叠为占位——证据账本里
   **不放 prompt 原文与完整 diff**。

## Why

- **审计要能回答「凭什么」**：`claim.evidence_ids` + `Validations[].evidence_ids`
  让每条结论可回溯到补丁/测试/oracle 事实，而不是模型的一句话。
- **分离是为了防「一句话变成事实」**：把解释写进证据层，是陈旧知识与幻觉
  进入 prompt 的主要通道（v8 波次里 F4「毒化检索块」的同类问题）。
- **确定性 id 让重放安全**：NATS 至少一次、worker 重启、事件重放都会重复投递
  同一份事实；`short_digest` 幂等把重复写成同一记录，而不是重复证据。
- **理由词表是指标的基础**：没有稳定的拒绝原因，就没有「stale 误注入 = 0」
  「未授权提升 = 0」这两个验收门槛，也无法在 P4/P5 做 dashboard 聚合。
- **集成/推送条件是「事实」而不是「意图」**：只有 merge receipt + push verdict
  才能证明内容真的进了目标分支（沿用 effect ledger 的 UNKNOWN≠成功纪律）。
- **gate 不是权限系统**：它只回答「这条 claim 能不能被读/被晋升」，与 sandbox
  工具权限（§8 不变量 4）正交，避免出现两套互相矛盾的授权。

## Boundary (what this decision does not do)

- 不自己判断 claim 的真假：只消费 oracle / test runner / reviewer / human 的
  `ValidationRecord`。
- 不做检索排序、不做摘要、不生成 prompt 内容（那是 recovery view / P4/P5 的事）。
- 不修改 claim 正文（除 supersede / retract / lock 的状态与原因字段）。
- 不引入第二套权威存储：三类记录都在 `RuntimeState`
  （`knowledge_evidence` / `knowledge_claims`，D1）。
- 不在 stage < C 时拒绝读取：SHADOW 阶段只记录判定结果，不改变现有行为（D5）。
- 不覆盖跨租户合规策略（`KnowledgeScope.allows_read` 是唯一裁决点，扩展策略
  需另开 decision）。

## Implementation ticket

证据账本：T07；认知门：T08 → [spec-p2.md](../spec-p2.md)
（`tickets/07-evidence-ledger.md`、`tickets/08-epistemic-gate.md`）。
