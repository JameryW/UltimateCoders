# 12 — 合并 / 推送收据入证据账本 (T12)

**What to build:** 把「内容真的进了目标分支」变成可查证据：workspace release 的
合并结果与 push 裁决写成 `MERGE_RECEIPT` / `PUSH_VERDICT`，供
`GRAPH → REPOSITORY` 晋升（D4 第 4 条）与审计使用。

**Blocked by:** T07（账本）、T08（门的集成/推送条件）、D1（权威存储）
**Spec:** [spec-p3.md](../spec-p3.md)
**Status:** implemented（采集：`python/ultimate_coders/agent/worker.py::
_record_delivery_evidence`；orchestrator 角色的合并侧：
`python/ultimate_coders/agent/merge_arbiter.py::_record_integration` →
`knowledge/integration.py::record_integration`。worker 以
`visibility=attempt` 写 `MERGE_RECEIPT` / `PUSH_VERDICT`；arbiter 为每个合并分支
以 `visibility=repository` 写收据，stage `canonical` 且有 graph scope 时校验 PASS
并晋升 REPOSITORY。`tests/python/test_merge_arbiter_knowledge.py` 13 tests）

**Implementation**

1. 采集点：`merge_result = await self._workspace_manager.release(...)` 之后
   （冲突/投递失败也走同一路径，因为 release 总是被调用），紧接着
   `_record_delivery_evidence(subtask, attempt, merge_result)`。
2. `EvidenceKind.MERGE_RECEIPT`：`payload={"status", "commit_sha",
   "branch_preserved"}`、`summary="workspace release"`；`status` 就是
   release 的裁决（成功 / `conflict` / 投递未确认），`commit_sha` 是目标接受后
   的提交，`branch_preserved` 记录分支是否仍被保留——三者都来自
   `WorkspaceManager.release()` 的返回值，不是 worker 自己的成功声明。
3. `EvidenceKind.PUSH_VERDICT`：仅当 `merge_result` 带 `push_status` 时写，
   `payload={"push_status"}`、`summary="branch push"`；
   `UNKNOWN` 原样保留（gate 的 `push_unknown` 依赖它，缺省成功是不允许的）。
4. 门禁与失败语义：`rollout.capture_enabled(stage)` 为假零写入；best-effort，
   异常只 `logger.debug`，不影响子任务结果。
5. 晋升：生产者一侧由 `merge_arbiter.py::_record_integration` 发起（见
   `knowledge/integration.py`）：stage `canonical` 时为合并分支写仓库级 claim 并用
   收据构造 `RevisionBinding(integration_ref, integration_sha)` 晋升 REPOSITORY；
   `push_status="UNKNOWN"` 必然被 `push_unknown` 拒绝，拒绝不改记录。

**Acceptance gates**

- [x] merge 成功 / 冲突路径都写 `MERGE_RECEIPT`，且 `status` 可区分。
- [x] 有 `push_status` 时写 `PUSH_VERDICT`；无则只写合并收据。
- [x] `UNKNOWN` 不被改写成成功（payload 原样保留 `str(push_status)`）。
- [x] stage A（默认）零写入；stage ≥`shadow` 才采集；采集失败不影响子任务。
- [x] 证据不含 diff / prompt（payload 只有三项/一项短字符串）。
- [x] **`GRAPH → REPOSITORY` 晋升调用点已实现**：`MergeArbiter` 在基分支 push
      之后调用 `record_integration`，以 `RevisionBinding(branch, revision=
      integration_sha, integration_ref="origin/<base>", integration_sha)` 作为
      integration 绑定；stage `canonical` 时写仓库级 claim、校验 PASS
      （`strategy="integration-receipt"`）并晋升 REPOSITORY。`UNKNOWN` 推送仍被
      `push_unknown` 拒绝，拒绝不改记录。

**Rollout:** 采集随 `UC_KNOWLEDGE_STAGE`≥`shadow`；晋升强制在 stage ≥`graph`。
默认关。
**Risks:** `MergeArbiter` 在 orchestrator 侧合并、push 在 release 里发生，
两者不是同一个进程；当前收据挂在**执行该 attempt 的 worker** 的 attempt 身份上，
`commit_sha` 才是跨进程可核对的锚点。若以后由 arbiter 侧另写收据，需保证
`(attempt, commit_sha)` 不产生两条互相矛盾的 `MERGE_RECEIPT`（幂等键已含
payload，同内容同 id）。
