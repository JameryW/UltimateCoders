# 04 — 补丁链记录与 diff 解析 (T04)

**What to build:** attempt 级不可变补丁记录 + git diff 解析器 + 按 attempt 身份
幂等的账本。这是「上一轮改了什么」的唯一事实来源。

**Blocked by:** D3（补丁轮次身份与不可变语义）
**Spec:** [spec-p1.md](../spec-p1.md)
**Status:** implemented（`python/ultimate_coders/knowledge/attempt_chain.py`；
focused cases in `tests/python/test_attempt_chain.py`）

**Implementation**

- 类型：`SymbolChange(path, symbol="", kind="line", start_line=0, end_line=0)`；
  `FilePatch(path, change_type="modified", old_path="", additions=0, deletions=0,
  diff="", truncated=False, symbols=())`（`summary()` 日志安全、`tokens()` 供预算）；
  `AttemptRound(attempt, outcome="unknown", patches=(), base_revision="",
  head_revision="", summary="", failure="", test_verdict="", created_at="",
  record_version=0)`（`key()` / `is_failure()` / `file_count` / `changed_paths`）。
- `parse_git_diff(text, *, max_files=None, max_diff_chars=None)`：
  `diff --git` 分段解析、rename 保留目标路径、二进制/超限 `truncated=True`、
  Git 引号路径（含八进制转义）还原；默认上限取 `rollout.max_patch_files()` /
  `rollout.max_patch_diff_chars()`。
- `AttemptChainLedger(state: RuntimeState)`，命名空间
  `NAMESPACE = "attempt_chain"`：`record()`（`_fingerprint()` 相同 → 返回已存；
  不同 → `AttemptChainConflictError`）、`get()`、`rounds(*, task_id, subtask_id,
  limit=50)`、`prior_rounds(current, *, limit=20)`、`latest()`、`clear()`；
  扫描上限 `_MAX_SCAN = 5000` 条。

**Acceptance gates**

- [x] 新增/修改/删除/重命名/二进制/超限截断/带空格与引号路径均有解析用例
      （`tests/python/test_attempt_chain.py`）。
- [x] `record()` 幂等：同键同内容不新增、返回已存记录且 `record_version` 稳定。
- [x] 同键不同内容抛 `AttemptChainConflictError`（不覆盖历史）。
- [x] `prior_rounds()` 按 `AttemptRef.order()` 升序、同 subtask 限定、去重；
      `latest()` 与 `clear()` 行为有断言。
- [x] 账本只依赖 `RuntimeState`（无新存储，D1）。

**Rollout:** 采集由 `UC_KNOWLEDGE_STAGE` 控制（≥B 才写）；本票不改变读路径。
**Risks:** diff 是**有界文本**不是可重放补丁（`truncated=True` 时必须承认不完整）；
账本体积随 attempt 数增长，读取一律 `limit` 有界。
