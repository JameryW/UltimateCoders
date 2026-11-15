# Spec P1 — Attempt Diff-Chain：把「上一轮改了什么」变成事实

Blocked by: D3（已决定）。Builds on P0 的 `AttemptRef` / `rollout` 开关与
统一编译面（[spec-p0.md](spec-p0.md)）。

## Goal

让失败重试不再盲重放：每次 attempt 在 sandbox 返回后、workspace 释放前，把
「改了哪些文件 / 哪个符号 / 改前改后修订 / 失败文本」记成一条**不可变、
按 attempt 身份幂等**的记录。这是 P3 Recovery View 的唯一事实来源，也是指标
「重复失败签名率」的分母。

## What to build

1. **记录类型（`python/ultimate_coders/knowledge/attempt_chain.py`）**：
   - `SymbolChange(path, symbol="", kind="line", start_line=0, end_line=0)`；
   - `FilePatch(path, change_type="modified", old_path="", additions=0,
     deletions=0, diff="", truncated=False, symbols=())`（`summary()` 日志安全、
     `tokens()` 供视图预算）；
   - `AttemptRound(attempt, outcome="unknown", patches=(), base_revision="",
     head_revision="", summary="", failure="", test_verdict="", created_at="",
     record_version=0)`，派生 `key()` / `is_failure()` / `file_count` /
     `changed_paths`。
2. **解析器**：`parse_git_diff(text, max_files=None, max_diff_chars=None)`
   解析 `diff --git` 分段（rename 保留目标路径、二进制/超限置 `truncated`、
   Git 引号路径还原）；上限来自 `rollout.max_patch_files()` /
   `rollout.max_patch_diff_chars()`。
3. **符号定位**：`locate_symbols(path, diff, *, source_text="", max_symbols=20)`：
   `.py` 走 `ast`（dotted class/def、最内层节点、hunk 行区间），其它语言/无源码
   回退 `kind="line"`；**永不抛异常**。每轮最多 `MAX_ROUND_SYMBOLS = 32` 个符号。
4. **账本**：`AttemptChainLedger(state: RuntimeState)`，命名空间
   `attempt_chain`：`record()`（同键同内容幂等、不同内容抛
   `AttemptChainConflictError`）、`get()`、`rounds(*, task_id, subtask_id,
   limit=50)`、`prior_rounds(current, *, limit=20)`、`latest()`、`clear()`。
5. **采集适配器（worker 侧）**：`WorkspaceManager.head_sha(handle)` 与
   `WorkspaceManager.capture_diff(handle, *, baseline="", max_files=20,
   max_diff_chars=20000)`（`agent/workspace.py`）——只读 git 查询，返回
   `{"baseline","head","files":[{"path","change_type","diff"}],"file_count",
   "truncated"}`；未跟踪文件合成 add patch（>512 KB 跳过）。
6. **接线（`agent/worker.py`）**：`_attempt_ref()` 用
   `task_id/subtask_id/dispatch_retry_count/attempt/worker_id` 构造身份；
   `_record_attempt_round()` 在 `_execute_in_sandbox` 返回后、`release()`/`quarantine()`
   之前调用；`test_verdict` 记为 `"unknown"`（不伪造 oracle 结论，原则 A）；
   采集失败只 `logger.debug`，绝不影响 subtask。
7. **默认关闭**：`_record_attempt_round()` 先查 `rollout.stage_from_env()`，
   `capture_enabled` 为假（`UC_KNOWLEDGE_STAGE` 未设 = A）直接返回——Stage A
   执行路径逐字节不变。

## Acceptance criteria

- [x] `parse_git_diff` 覆盖新增/修改/删除/重命名/二进制/超限截断与引号路径
      （`tests/python/test_attempt_chain.py`）。
- [x] `record()` 幂等：同键同内容返回已存记录、不新增；同键不同内容抛
      `AttemptChainConflictError`；版本 CAS 冲突有测试。
- [x] `prior_rounds()` 按 `AttemptRef.order()` 升序、限定同一 subtask、
      去重后返回；`latest()` / `clear()` 行为有测试。
- [x] `locate_symbols()` 对 `.py` 给出 dotted symbol 与行区间，对非 Python /
      语法错误文件回退行号且不抛异常（`tests/python/test_attempt_chain.py`）。
- [x] `capture_diff()` 只读：不产生 commit/stage/reset；未跟踪文件、rename、
      超限截断、空 diff 均有断言（`tests/python/test_workspace_patch_capture.py`）。
- [x] worker 采集点位于 sandbox 返回之后、workspace 释放之前，且
      `UC_KNOWLEDGE_STAGE` 未设时不采集（`tests/python/test_attempt_chain.py`
      之外的接线由 `test_recovery_view.py` 的 stage 断言间接覆盖）。
- [ ] 端到端：一条真实失败重试链在 `attempt_chain` 命名空间留下 N 条 round，
      且 `attempt` 维度在重派发时（`dispatch_retry_count>0`）不与 attempt 0 撞键
      （接线已就位，等 P3 的恢复视图联调后一起验收）。

## Out of scope

- 失败签名归一化与重复失败判定（P2 的 `FAILURE_SIGNATURE` 证据）。
- 跨 worker / 跨任务读取、保留期清理、知识页聚合（P4/P5）。
- 非 Python 的 AST 级定位；JS/TS 本阶段只到行号。
