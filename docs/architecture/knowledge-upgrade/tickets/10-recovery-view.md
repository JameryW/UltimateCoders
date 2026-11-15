# 10 — 有界恢复视图：事实与假设分离 (T10)

**What to build:** 一个纯函数，把「同一 subtask 链上最近几轮的补丁/结局/校验事实」
渲染成有界文本，并把事实与假设拆成两个不同信任级的 ContextItem——这是第一次
让重试 agent 看到「已经试过什么、为什么失败」。

**Blocked by:** T04（补丁链）、D6（预算）、T02（预算默认值）
**Spec:** [spec-p3.md](../spec-p3.md)
**Status:** implemented（`python/ultimate_coders/knowledge/recovery.py`；
focused cases in `tests/python/test_recovery_view.py`，11 passed）

**Implementation**

- 常量：`RECOVERY_SOURCE = "recovery-ledger"`、
  `RECOVERY_TITLE = "## Prior attempt evidence"`、`MAX_PATHS_PER_ROUND = 6`、
  `MAX_ROUND_DETAIL = 240`、`DEFAULT_MAX_ROUNDS = 5`、`MAX_HYPOTHESES = 3`。
- `RecoveryView(attempt, rounds=(), evidence=(), hypotheses=(), facts_text="",
  hypotheses_text="", summary={})`：`.text`（事实段 + 空行 + 假设段）、
  `.is_empty()`、`.tokens()`、`.to_dict()` / `.from_dict()`。
- `build_recovery_view(*, attempt, rounds=None, evidence=None, hypotheses=None,
  budget_tokens=None, max_rounds=None)`：纯函数、不抛异常；预算默认
  `rollout.recovery_budget_tokens()`（`UC_RECOVERY_BUDGET_TOKENS`，默认 600，
  clamp 128–2000），字符预算 = `budget * 4`；取最近 `max_rounds`（默认 5）
  轮，新轮优先，装不下丢最旧并置 `truncated=True`；假设先渲染但**事实优先
  占预算**（超出则 `hypotheses_text=""`）。
- 渲染：每轮 `### Attempt {retry} (dispatch {dispatch}) of subtask {subtask[:8]}
  — {outcome}` + `Changed:`（≤`MAX_PATHS_PER_ROUND` 路径）+ `Revision: base ->
  head` + `Failure:` / `Verification:` / `Summary:`（`safe_text` 到
  `MAX_ROUND_DETAIL`）。
- `recovery_context_items(view, *, source=RECOVERY_SOURCE)`：两个
  `Tier.EVIDENCE` 项——事实 `recovery-facts:{attempt.key()}` /
  `trust_class="attempt_ledger"` / `verified=True` / `relevance=1.0`；假设
  `recovery-hypotheses:{attempt.key()}` / `trust_class="unverified_hypothesis"` /
  `verified=False` / `relevance=0.6`。
- `summary`（log-safe）：`attempt` / `rounds_shown` / `rounds_available` /
  `evidence_records` / `hypotheses_shown` / `tokens` / `budget_tokens` /
  `truncated`。

**Acceptance gates**

- [x] 纯函数：同输入同输出，不读外部状态、不抛异常（`test_recovery_view.py`）。
- [x] 预算不足时丢最旧并置 `truncated`；`summary` 键齐全、数值自洽
      （`tokens ≤ budget_tokens`）。
- [x] 事实与假设拆两个 item，假设不继承 `verified`；超预算时假设先让位。
- [x] 空输入返回 `is_empty()` 视图 + 空 item 列表（调用方据此不重编）。
- [x] 默认预算来自 `rollout.recovery_budget_tokens()`，不 hardcode 600。

**Rollout:** 纯函数本身无开关；只有 stage ≥ `recovery` 时 worker 才会调用它
（见 T11）。默认关。
**Risks:** 600 token 是实验起点（D6），过大反而稀释首次尝试信息；`MAX_ROUNDS=5`
在长链任务上会丢早期轮次——用 `rounds_available` 与 `truncated` 观测后再调。
