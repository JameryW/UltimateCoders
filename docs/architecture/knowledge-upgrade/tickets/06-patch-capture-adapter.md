# 06 — 补丁采集适配器与 worker 接线 (T06)

**What to build:** 在 workspace 仍持有 worktree 时只读地抓取本次 attempt 的候选
diff，并把它接进重试环，保证「sandbox 返回之后、release 之前」一定留下补丁事实。

**Blocked by:** D3；T04（账本）、T02（上限默认值）
**Spec:** [spec-p1.md](../spec-p1.md)
**Status:** implemented（`python/ultimate_coders/agent/workspace.py` 的
`head_sha()` / `capture_diff()`、`python/ultimate_coders/agent/worker.py` 的
`_attempt_ref()` / `_record_attempt_round()` / `_worktree_source()`；focused
cases in `tests/python/test_workspace_patch_capture.py`，4 passed）

**Implementation**

- `WorkspaceManager.head_sha(handle) -> str`：`git rev-parse HEAD`，失败返回 ""。
- `WorkspaceManager.capture_diff(handle, *, baseline="", max_files=20,
  max_diff_chars=20000) -> dict`：返回
  `{"baseline","head","files":[{"path","change_type","diff"}],"file_count",
  "truncated"}`；对 `merge-base(base_branch, HEAD)`（或调用方给的 baseline）
  执行 `git -c core.quotepath=false diff --no-color --no-ext-diff`，
  `git status --porcelain --untracked-files=all` 识别新增/删除/改名，未跟踪
  文件合成 add patch（单文件 >512 KB 跳过并留占位），**只读**（不 stage /
  commit / reset）。
- worker 接线：`_attempt_ref()` 用 `subtask.parent_id` / `subtask.id` /
  `dispatch_retry_count` / `attempt` / `worker_id` 构造 `AttemptRef`；
  `_record_attempt_round()` 在 `_execute_in_sandbox()` 返回后、`release()` /
  `quarantine()` 之前调用；`outcome` = `completed` / `failed` /
  `cleanup_pending`；`test_verdict="unknown"`（不伪造 oracle 判定）；
  `locate_symbols()` 用 `_worktree_source()` 读取改后源码；异常只
  `logger.debug`。
- Stage A（`UC_KNOWLEDGE_STAGE` 未设）直接返回，执行路径逐字节不变。

**Acceptance gates**

- [x] 修改 / 新增 / 删除 / 改名 + 未跟踪文件都出现在 `files[]`，`change_type`
      正确映射（`tests/python/test_workspace_patch_capture.py`）。
- [x] `max_files` / `max_diff_chars` 截断生效并置 `truncated=True`。
- [x] `capture_diff()` 不改变 git 状态（测试前后 `git status` 一致 / 无新 commit）。
- [x] worker 采集点位置正确：失败 attempt 也留下 round；`cleanup_pending`
      （超时需清理）也记录。
- [ ] 重派发（`dispatch_retry_count>0`）与 in-worker retry 的组合键在真实链路
      上不撞键：已由 `_attempt_ref()` 语义保证，端到端验收随 P3 联调完成。

**Rollout:** `UC_KNOWLEDGE_STAGE`≥`shadow` 时采集；默认关。
**Risks:** 大仓库全量 `git status` 有成本（仅在 sandbox 返回后一次）；worktree
已被清理时只能拿到空快照，属预期降级而非错误。
