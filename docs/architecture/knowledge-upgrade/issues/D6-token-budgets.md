# D6 — token 预算是实验起点，不是验收承诺

Status: decided
Decided: 2026-11-15

## Question

用户计划给出三组数字：planner 1500–1800、worker 2000–2500、retry recovery 300–600。
它们是必须满足的产品承诺，还是先跑量化实验的起点？和已有的
`UC_CONTEXT_BUDGET_TOKENS`（默认 2000）是什么关系？文档、代码、验收各自该钉什么？

## Decision

**三组预算是实验起点（hypothesis），不是验收承诺；每个数字必须由 owning
constant / env 派生，文档只引用，不复述成「新默认」。**

1. **worker 单次编译**：`UC_CONTEXT_BUDGET_TOKENS`，默认 2000
   （`python/ultimate_coders/agent/worker.py::_context_budget_tokens`）；实验区间
   2000–2500。P2 证据层与 P0 策略层受 `_RESERVED_TIERS` / risk 预留保护，**不得**
   为省 token 先砍证据层。
2. **planner 规划视图**：1500–1800 是起点；planner 侧视图由 Rust gateway 组装的
   `ContextBlock` 决定（`crates/uc-types/src/envelope.rs`），现有 env 开关
   `UC_PLANNER_VIEW_BUDGET_BYTES`（默认 8192 = `CONTEXT_BLOCK_MAX_BYTES` 硬上限，
   clamp 512–8192；1500–1800 tokens ≈ 6–7.2 KiB 落在可调区间内，改 env 即可做 A-B，
   不需要改代码）。硬上限不随 env 放开。
3. **retry recovery**：`UC_RECOVERY_BUDGET_TOKENS`，默认
   `DEFAULT_RECOVERY_BUDGET_TOKENS = 600`（`knowledge/rollout.py`），clamp
   128–2000；实验区间 300–600。
4. **补丁体积**：`UC_KNOWLEDGE_MAX_PATCH_FILES`（默认 20，clamp 1–200）与
   `UC_KNOWLEDGE_MAX_DIFF_CHARS`（默认 20_000，clamp 500–200_000），同属
   `knowledge/rollout.py`；超限只截断并标 `truncated=True`，不报错、不影响执行。
5. **验收看指标表不看压缩率**：任务成功率、平均 attempt 数、重复失败签名率、
   tokens/task、compile P95、证据采集完整度、stale-memory 误注入=0、未授权提升=0
   （表在 [spec-p0.md](../spec-p0.md) §验收指标）。token 与延迟只作「不劣化」护栏。
6. **默认值唯一住址**：所有默认/上限都在 `knowledge/rollout.py`；新增数字必须先有
   constant，再写进文档。

## Why

- 用户计划明确写了「token 预算是实验起点」。在指标尚未测出前把它写成承诺，会把
  错误配置锁死，还会诱导实现为了达标砍证据。
- 预算分散在文档/代码/测试三处必然漂移（v8 波次已经踩过「文档数字 vs 实测」的
  ticket 21/22）。集中到 `rollout.py` 后，clamp 与默认值只改一处。
- 恢复视图预算是 Stage C 的准入：没有 `UC_RECOVERY_BUDGET_TOKENS` 的上限，always
  注入 attempt 1 的全部轨迹会把 prompt 撑爆，反而掩盖「有界」这件事。

## Boundary（本决策不做什么）

- 不新增第二套 planner 预算常量；开关只调既有 `ContextBlock` 预算（T17），
  8 KiB 硬上限不变。
- 不承诺 2000 / 600 是全局最优，只承诺它们是当前默认。
- 不改 `budget.py` 的 `TaskBudgetLedger`（任务级 token/cost/time 预算，是另一层：
  `UC_TASK_TOKEN_BUDGET` / `UC_TASK_COST_BUDGET_USD` / `UC_TASK_TIME_BUDGET_SECS`）。
- 不把 prompt 预算用于计费或准入拒绝（准入归 `TaskBudgetLedger`）。

## Implementation ticket

T10（有界 Recovery View，`UC_RECOVERY_BUDGET_TOKENS`）、T04（`parse_git_diff` 的
`max_files` / `max_diff_chars` 截断）。
