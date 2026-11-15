# 11 — 重试环注入与可观测 (T11)

**What to build:** 把恢复视图接进 worker 的重试环：第 2 次及以后的 attempt 用
**同一个预算、同一份 manifest** 重新编译 context，而不是追加一段第二预算的文本；
同时把「本轮注入了什么」做成 log-safe 摘要随事件上报。

**Blocked by:** T10（视图）、T03（编译面）、T04（账本读）
**Spec:** [spec-p3.md](../spec-p3.md)
**Status:** implemented（`python/ultimate_coders/agent/worker.py::
_load_recovery_view` + `_recovery_context_block`，重试环 1168 行起调用；
`subtask_retry` 事件在重试决策点（约 1418 行）计算并携带 `data["recovery"]`；
视图层由 `tests/python/test_recovery_view.py` 覆盖；A-B 测量 harness 已就绪
（`knowledge/metrics.py`，T15）。2026-11-15 失败注入实测：两臂各 3 次失败
attempt，C 的 `subtask_retry` 事件带回递增的 recovery 摘要（1→2 轮、
95→184 token / 600 预算），B 为 `None`，整条重试链无异常；记录见
[../runbook-ab-evaluation.md](../runbook-ab-evaluation.md) §8）

**Implementation**

- `_load_recovery_view(subtask, attempt) -> RecoveryView | None`：stage <
  recovery 或 `attempt <= 0` 返回 `None`；读
  `AttemptChainLedger.prior_rounds(current, limit=rollout.chain_max_rounds())`，
  无历史 / 读取失败也返回 `None`（调用方保持首次尝试的 prompt 逐字不变）。
- `_recovery_context_block(subtask, attempt, base_items, current_block, *,
  legacy_style, gateway_truncated) -> (context_block, recovery_summary)`：
  1. `view = await self._load_recovery_view(...)`；`None` → 原样返回
     `(current_block, {})`；
  2. `recovery_context_items(view)`；渲染失败或空列表 → 原样返回；
  3. `compile_context(list(base_items) + items, budget_tokens=
     self._context_budget_tokens(), risk=self._last_route.risk)` 重新编译；
     `render_legacy(legacy_style=..., gateway_truncated=...)` 输出；
     `self._last_context_manifest = manifest.summary()`（同一 dispatch key 不重复
     落库）；
  4. 返回 `(block, view.summary)`；异常全部吞掉并回退原 block。
- 重试环顺序：`attempt > 0` → 重新路由 → `_recovery_context_block` →
  （必要时）重新 acquire workspace → `_execute_in_sandbox`。
- 事件：重试决策点先 `next_view = await self._load_recovery_view(subtask,
  attempt + 1)`（此时本 attempt 的补丁轮已落盘，所以能如实预告「下一次会带
  什么」），`subtask_retry.data` 增加
  `**({"recovery": recovery_summary} if recovery_summary else {})`——只有
  计数/预算/是否截断，不含内容。
- `subtask_started` / manifest 审计仍走既有 `_record_context_manifest()`；
  恢复视图是**同一份** manifest 的一部分，不是额外一条。

**Acceptance gates**

- [x] stage A/B（`UC_KNOWLEDGE_STAGE` 未设或 `shadow`）下 `context_block` 与首次
      尝试逐字相同（`recovery_enabled` 早退）。
- [x] 无先前轮次时不重编、不产生 `recovery` 字段。
- [x] 重编只发生一次 `compile_context`（无第二预算、无追加渲染）。
- [x] `recovery` 摘要 log-safe：只含 `summary` 键，不含补丁/失败原文。
- [x] **A-B 测量已可执行**：`knowledge/metrics.py` + CLI（T15）按 task 配对
      baseline/variant，输出重复失败签名率、平均 attempt 数、成功率与 tokens 的
      判定（`improved` / `non_inferior` / `regressed` / `inconclusive` /
      `insufficient_data`）；见 [spec-p0.md](../spec-p0.md) §评测协议。
      真实失败重试已验证恢复视图注入（runbook §8）；**决策级 B/C 数据未做**
      （Frontier）：数据出来前不得宣称改善。
- [ ] 超时/崩溃路径的恢复视图读取（worktree 已 quarantine 时
      `prior_rounds()` 仍应可用）需在联调中确认。

**Rollout:** `UC_KNOWLEDGE_STAGE=recovery`（或 `2`/`canary`）才改变 prompt；
回滚 = 清掉该 env。默认关。
**Risks:** 重编会让缓存前缀变化（`render_parts()` 的 stable 段仍稳定，但
volatile 段变长）——缓存命中率下降是可接受成本，但不能让 prompt 超预算；
已用 risk 预留的 P2 段保护事实。
