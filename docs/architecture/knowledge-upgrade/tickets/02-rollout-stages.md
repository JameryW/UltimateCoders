# 02 — 分阶段开关与预算默认值 (T02)

**What to build:** 一个只住 env 解析与上限默认值的模块，让整条波次可灰度、
可回滚、默认关闭，并把所有魔法数字集中到一处（tickets / specs 只引用常量）。

**Blocked by:** 无（D5 / D6 已决定）
**Spec:** [spec-p0.md](../spec-p0.md)
**Status:** implemented（`python/ultimate_coders/knowledge/rollout.py`；
`tests/python/test_rollout.py` 覆盖 A–E 别名、未知值 fail-closed 与各上限 clamp）

**Implementation**

- `KnowledgeStage(IntEnum)`：`DISABLED=0` / `SHADOW=1` / `RECOVERY=2` /
  `GRAPH=3` / `CANONICAL=4`；`resolve_stage()` 接受 A–E 别名与 0–4，
  未知值/非法整数 **fail-closed 到 `DISABLED`**；`stage_from_env(env=None)`。
- 能力谓词：`capture_enabled`（≥SHADOW）、`recovery_enabled`（≥RECOVERY）、
  `graph_sharing_enabled`（≥GRAPH）、`canonical_enabled`（≥CANONICAL）、
  `describe_stage()` → `{stage, capture, recovery, graph, canonical}`。
- env 与默认值（默认值只住本文件）：
  `UC_KNOWLEDGE_STAGE`（未设 = `DISABLED`）、
  `UC_RECOVERY_BUDGET_TOKENS`（默认 600，clamp 128–2000，`recovery_budget_tokens()`）、
  `UC_ATTEMPT_CHAIN_MAX_ROUNDS`（默认 20，clamp 1–100，`chain_max_rounds()`）、
  `UC_KNOWLEDGE_MAX_PATCH_FILES`（默认 20，clamp 1–200，`max_patch_files()`）、
  `UC_KNOWLEDGE_MAX_DIFF_CHARS`（默认 20 000，clamp 500–200 000，
  `max_patch_diff_chars()`）。

**Acceptance gates**

- [x] `resolve_stage()` 未知值一律 `DISABLED`；`stage_from_env()` 走同一解析；
      `describe_stage()` 与四个谓词一致。
- [x] 数值型 env 非法/越界时回落到默认并 clamp，不抛异常（`_clamped_int()`）。
- [x] 默认部署（不设 `UC_KNOWLEDGE_STAGE`）下整条链路 no-op。
- [x] 独立测试文件 `tests/python/test_rollout.py` 覆盖 A–E 别名、未知值 fail-closed、
      各上限 clamp 与默认值。

**Rollout:** 本票就是开关本身；默认 A `DISABLED`。
**Risks:** 环境变量拼写错误会静默回到 `DISABLED`（预期行为，但要在上线手册里
写明「以为开了其实没开」的排查方法：看 `describe_stage()` 输出）。
