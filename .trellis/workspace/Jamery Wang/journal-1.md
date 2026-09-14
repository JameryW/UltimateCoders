# Journal - Jamery Wang (Part 1)

> AI development session journal
> Started: 2026-07-29

---



## Session 1: Scheduler job pause/resume

**Date**: 2026-09-08
**Task**: Scheduler job pause/resume
**Branch**: `main`

### Summary

Added the scheduler's missing reversible stop: SchedulerService::set_job_enabled pauses a job (unregister from the runtime scheduler, clear next_execution, keep last_execution + execution_history) and resumes it (recompute from now, re-register), surfaced end to end via EngineApi::set_scheduler_job_enabled, gRPC DashboardService::SetSchedulerJobEnabled, regenerated TS stubs, uc_scheduler pause/resume, /uc schedule pause|resume, and the dashboard hook + SchedulerPanel control. Also fixed the latent restart hole where start() recovered only list_tasks(true), so a disabled job vanished from the registry after a gateway restart and could never be activated or cleaned up; recovery now loads every persisted task and registers only enabled ones. Delivered through issue-flow as spec #625 -> ticket #626 (native blocked-by edge), Trellis task 09-07, and PR #627 (15 CI checks green, squash-merged). Known gaps left open: SchedulerPanel is still unmounted by the live dashboard, and multi-gateway live-read consistency stays out of scope.

### Main Changes

(Add details)

### Git Commits

| Hash | Message |
|------|---------|
| `12b8cf6` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 2: T1: execution envelope + contract_version handshake (#637)

**Date**: 2026-09-12
**Task**: T1: execution envelope + contract_version handshake (#637)
**Branch**: `main`

### Summary

Stamped graph/node/attempt/idempotency/epoch/contract envelope on ALL THREE uc.subtask.execute publishers (incl. Python _dispatch_remote found by check); contract_version handshake refuses mismatched registration/heartbeat and gates dispatch (legacy workers accepted-but-never-dispatched, warn-only per accepted deviation). Rust 428/370/180+8/35, clippy clean, pytest 977. Both TS stubs regenerated. Next: T2 #638 unblocked (new context).

### Main Changes

(Add details)

### Git Commits

| Hash | Message |
|------|---------|
| `e1f207e8` | (see git log) |
| `215e11d1` | (see git log) |
| `30bba2e0` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 3: T2: graph row tables + one-shot import (#638)

**Date**: 2026-09-13
**Task**: T2: graph row tables + one-shot import (#638)
**Branch**: `main`

### Summary

Five-table GraphStore (incl. reserved cost/tokens/duration + schema-only node_completions) on the #631 advisory-lock template; idempotent dual-source backfill (PG JSONB auto + opt-in .uc/tasks newer-savedAt); warn-only shadow plane default off, HashMap still behavioral authority till T6. Check fixed backfill fatal-abort on malformed rows and NOW() determinism hole. Gates 435/377/182+8/36/28, clippy both modes clean, 7 real-PG integration tests actually executed.

### Main Changes

(Add details)

### Git Commits

| Hash | Message |
|------|---------|
| `28b66e92` | (see git log) |
| `8f82b7bb` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 4: T3: graph state machine + NodeCompletion commit-once + attempt lifecycle (#639)

**Date**: 2026-09-13
**Task**: T3: graph state machine + NodeCompletion commit-once + attempt lifecycle (#639)
**Branch**: `main`

### Summary

图平面长出真实转移逻辑：uc-types 9 态 NodeStatus + 纯转移表，uc-engine 六个单事务 CAS 转移方法（commit-once 靠 node_completions PK 的 ON CONFLICT DO NOTHING 行数定胜负），网关四个 sink 动词挂在派发/结果/reaper 三个路点。调度动词复用两个派发口共用的 Assigned 标记点，reaper 用 pre-increment attempt id。门禁 fmt + clippy 双 feature 全绿、测试 437/379/187+8/36/34（T2 基线 435/377/182+8/36/28，只增不减），17 个真 PG ignored 测试实跑全过（T2 的 7 个 + 本票 10 个）。legacy HashMap 与 reaper 三测试未动，T6 切主时再删。解锁 T4 #640 与 T6 #642 的图面。

### Main Changes

- `crates/uc-types/src/graph.rs` (new) — `NodeStatus` 9 态枚举 + 纯函数 `can_transition()` 合法转移表（17 条边，无自环，终态无出边），大写 token serde 与 T2 起的 `graph_nodes.state` 词汇表字节一致；6 个单元测试双向钉住转移表。
- `crates/uc-engine/src/graph_store.rs` — 6 个转移方法，每个单事务 + `UPDATE … WHERE version=$n` CAS（受 `can_transition` 守卫）+ `execution_events` 追加（cost/tokens 留 NULL）：`schedule_attempt`（READY→SCHEDULED→RUNNING 合一，retry_no=max+1，worker_epoch 单调 fence）、`heartbeat_attempt`、`commit_once`（`INSERT … ON CONFLICT DO NOTHING` 行数定胜负，败者/被 fence 的迟到结果只记 `late_result` 事件不动状态）、`fail_attempt`（有预算则 epoch bump + node 回 READY，耗尽则 FAILED）、`timeout_sweep`（stale heartbeat 走 fail 路径）、`recompute_ready`（幂等，optional SKIPPED 视为满足）。锁序 graph→node→attempt。
- `crates/uc-grpc/src/server.rs` — `GraphShadowSink` 加 `on_schedule/on_heartbeat/on_commit/on_fail` 默认 no-op 方法；统一 `graph_verb()` 旁路扇出。schedule 挂在两个派发口共用的 `update_subtask_status` Assigned 标记点上（一次钩子覆盖 `publish_ready_subtasks` 与 `dispatch_ready_subtasks`，重复标记不产生新 attempt）；commit/fail 挂在 `apply_update_with_metadata` 终态派生后；300s stale-Assigned 与 stale-worker reaper 用 pre-increment attempt id 扇出 fail。legacy HashMap 突变与其 reaper 测试原样未动。
- `crates/uc-engine/tests/graph_store_integration.rs` — 新增 10 个真 PG 测试（双 commit 恰一 winner、commit 后迟到 FAIL 被 fence 且留 late_result、菱形下游不等兄弟、预算重挂/耗尽、heartbeat 刷新与终态 fence、同 version 双 CAS 一胜、recompute_ready 幂等、sink 动词走 trait object、timeout_sweep fence+rearm）。


### Git Commits

| Hash | Message |
|------|---------|
| `53983b1e` | (see git log) |
| `fa56066d` | (see git log) |
| `1857316b` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 5: T4 #640: idempotency closed loop + attempt dedup + late-result fencing

**Date**: 2026-09-14
**Task**: T4 #640: idempotency closed loop + attempt dedup + late-result fencing
**Branch**: `main`

### Summary

Implemented T4 items 3-5 (late-attempt fencing, stale-dispatch drops with user-visible counters, wire envelope-only flip), fixed the attempt-identity-chain bug (_build_subtask_from_data), repaired four stale tests, ran all gates (Rust 437/379/192+8/36/34, pytest 978 passed 0 regressions), archived the task and closed #640.

### Main Changes

# T4 (#640) session 1 — 2026-09-14: idempotency closed loop shipped

## What happened

Continued from the T4 working tree (items 1–2 from 09-13): implemented items 3–5, ran all gates, fixed four stale tests, split the work into four per-crate commits, archived the task, and closed #640.

- **Late-result fencing (item 3)**: `NatsSubtaskUpdate.attempt_id: Option<u64>` (serde default); worker `_make_task_update_payload` stamps `attempt_id = dispatch_retry_count`; gateway rejects partial updates whose stamped attempt is strictly older than the subtask's current one (no status/result/graph-verb change), bumps the counter, warns, and records a `stale_result_rejected` TaskUpdated event. Equal/greater passes; unstamped legacy updates and full snapshots bypass the fence.
- **Real bug found & fixed (attempt identity chain)**: `_build_subtask_from_data` never read `retry_count`/`attempt_id`, so Python `Subtask.dispatch_retry_count` was always 0 — the attempt-scoped checkpoint keys from item 2 all collapsed to attempt 0 on the real dispatch path. Now reads `retry_count ?? attempt_id` (int-coerced, degrades to 0) and threads through `_make_subtask_result_task`.
- **Stale-envelope drops (item 4, D7)**: worker terms envelope-less dispatches (no subtask_failed — distinct from the max_deliver cap path), counts `stale_dispatch_dropped`, publishes a user-visible `stale_dispatch_dropped` event (reused TaskUpdated surface instead of a new AgentEventType variant to keep the exhaustive-match blast radius at one arm). Gateway: `NatsHeartbeat.stale_dispatch_dropped` (serde default) → TaskStore monotonic max per consumer_id; getters; ListWorkers NATS-fallback metadata JSON. No proto change.
- **Wire flip (item 5, D3 lockstep)**: all three uc.subtask.execute publishers stop emitting `task_id`/`subtask_id`; workers parse graph-first with legacy fallback. Separate commit for independent rollback.

## Test fixes (intentional behavior changes)

- Three JetStream behavior tests used envelope-less fixtures → legitimately hit the new D7 stale branch (term + event). Fixtures upgraded to envelope-bearing payloads; `test_js_missing_ids_terminates` docstring updated to D7 semantics.
- `test_dispatch_remote_publishes_execution_envelope` asserted `task_id == graph_id` dual-write parity → KeyError after the wire flip; now asserts legacy-key absence + envelope fields.

## Commits

99176380 feat(grpc) / 04b6162b feat(engine) / 2041e683 feat(worker) / 5940e42 feat(wire) / 1c3f448 chore(task): archive. Hunk-level split was done via temp-state edits + staged adds (item 5 shares files with items 1/3/4 in server.rs, dispatcher.rs and nats_worker.py); intermediate C1/C2/C3 states were compile- and test-verified before committing.

## Gates

- fmt clean; clippy -D warnings clean on all five targets in both feature modes.
- Rust suites: 437 / 379 / 192+8 / 36 / 34 (T3 baseline 437/379/187+8/36/34 — only up).
- pytest: 978 passed / 4 skipped / 0 regressions. Four JetStream failures were real stale tests (fixed); merge_arbiter/workspace worktree failures are sandbox-environment noise — attributed by controlled experiments: (a) sandbox safe-delete bulk guard blocks seed-dir deletes (58–70 files > 50 threshold); (b) bypass + deep temp paths hit Windows MAX_PATH; bypass + shallow basetemp passes 11/11.
- NOT run this session: real-PG integration suite (Postgres down, Docker daemon stopped, service start needs admin — storage untouched by T4, suite was executed live at T3); real-JetStream tests loud-skip while local NATS is down (dedup probe-verified during research).

## Next

T5 #641 (Executor trait unification) then T6 #642 (authority flip; depends on T4's envelope-only wire).


### Git Commits

| Hash | Message |
|------|---------|
| `99176380` | (see git log) |
| `04b6162b` | (see git log) |
| `2041e683` | (see git log) |
| `5940e42` | (see git log) |
| `1c3f448` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 6: T5 #641 统一 Executor trait 落地完成（JetStream 硬依赖 / effect_class 白名单 / 分解分叉删除）

**Date**: 2026-09-14
**Task**: T5 #641 统一 Executor trait 落地完成（JetStream 硬依赖 / effect_class 白名单 / 分解分叉删除）
**Branch**: `main`

### Summary

T5 六提交落 main：Rust Executor trait/Selector/effect_class 投影 + 删 DispatchMode::Local 与分解分叉 + gateway 供给 UC_SUBTASKS + Python worker JS 硬依赖（拒注册/重试/可观测）。门禁全绿（Rust 437+5/379+5/192+8/36/35，pytest 977+4skip），无头 grep 验收通过，#641 关闭，任务归档 2026-09。

### Main Changes

# T5 #641 收尾 session — 统一 Executor trait 落地完成

日期：2026-09-14。T5 六个提交全部落 main，票关闭，任务归档。

## 交付内容（六提交）

1. `feca89f` feat(types)!: EffectClass 类型（read_only/local_safe/requires_worker，snake_case serde）+ 删 DispatchMode::Local + 下游机械修复（uc-grpc conversions match arm、字面量补字段）。
2. `c3d067f` feat(engine): Executor trait + NatsExecutor/LocalExecutor/Sandbox/Remote 占位 + ExecutorSelector 路由权威 + NodeRow.effect_class 投影 + tests/executor_nats_down.rs（5 测试：NATS-down 推进/READY+告警/WHAT 不变/up 全走 Nats/effect_class 投影/unwired StayReady）。
3. `91c7d2d` feat(engine)!: 删分解分叉 —— TaskStore::submit_task 改 insert-only（subtasks: Vec::new()），Python splice 删 decompose_task/strip_workflow_marker 等 185 行 + 9 个 decompose 测试。
4. `8db5ef8` feat(grpc): wire 携带 effect_class（NatsSubtaskExecute 字段 + payload），TaskStore 注释修正。
5. `4215799` feat(grpc-server): ensure_subtasks_stream —— gateway 侧启动时 get_or_create UC_SUBTASKS（WorkQueue/7d/120s dedup）。
6. `8cd7e01e` feat(worker)!: Python worker JS 硬依赖 —— 删 core-NATS 回退（queue="workers" 订阅 + _handle_subtask_execute）；新增 _ensure_subtask_transport 后台绑定循环（add_consumer(max_deliver=5) + pull_subscribe，失败 5s 重试）；transport 未就绪拒注册 _register_with_gateway 门禁 + 心跳 re-register；绑定成功立即注册；subtask_transport 进 registration metadata + 心跳 w_info；Python 删 DispatchMode.LOCAL（legacy "local" → PreferRemote）+ 编排器 LOCAL 分支；task_store.rs Subtask import 加 storage gate。

## 门禁终值

- fmt OK；clippy -D warnings：uc-types / uc-engine(默认+nodefault) / uc-grpc --all-features / uc-grpc-server 全绿。
- Rust 基线：uc-engine 437 lib + 5 executor 集成 + 17/18 ignored（PG/NATS 宕机）；nodefault 379+5；grpc 192+8；grpc-server 36；uc-types 35。T4 基线 437/379/192+8/36/34，只增不减 ✅。
- pytest 全量（bypass + 浅 basetemp）：977 passed + 4 skipped。T4 基线 978：净 -4 = 随 core 回退删除的 5 个测试（3 stream + 2 consumer + start 两个重写 + sandbox capability-reject 1 个）被 4 个新 transport 测试部分抵消，属预期删减。

## 验收对照（票面三条）

- (a) NATS-down：executor_nats_down.rs 覆盖 local_safe 图推进、coding 停 READY+告警、WHAT 字节级不变 ✅
- (b) JS 不可用：worker 拒注册（_register_with_gateway 门禁 + 心跳重试）+ 周期重试（5s，日志 1min 一次）+ subtask_transport 可观测（metadata + heartbeat）；测试替身 add_consumer/pull_subscribe 抛错镜像真实 JS 行为 ✅
- (c) 无头 grep：crates/ 与 python/ 中 decompose_task / DispatchMode::Local / queue="workers" / Falls back to core NATS / _handle_subtask_execute( 生产代码零匹配 ✅（orchestrator._decompose_task 为 default 模式 LLM 分解产品功能，非分叉，保留）

## 本 session 踩坑

- 同文件并行 Edit 丢失第三次复现风险区（本轮全部串行执行，未再发生）。
- 大块替换用 Python splice（splitlines(keepends=True) + 行号断言 + eol 感知）——注意 CRLF：锚点字符串必须按文件实际 eol 构造，否则 assert 失败（本轮 t5-splice-core-handler.py 首跑即此原因失败，未写盘，重跑成功）。
- nodefault 模式 unused import：storage-only 类型（Subtask row mapping）必须 cfg(feature="storage") 门禁导入——删除导入前要先想清楚双 feature 组合。
- CancelledError 语义：后台重试循环让取消传播（stop() 侧已有捕获），不要在循环内吞掉返回 False——否则测试断言 cancelled() 会失败。

## 状态

- 票 #641 关闭（close comment 附交付提交+门禁+验收 notes）。
- 任务目录归档 .trellis/tasks/archive/2026-09/09-14-t5-executor-trait/（task.json completed）。
- T6（切主 + 删 legacy reassign/reaper）就绪：依赖 T5 的 envelope-唯一身份源 + effect_class 白名单已全部落地。


### Git Commits

| Hash | Message |
|------|---------|
| `feca89f` | (see git log) |
| `c3d067f` | (see git log) |
| `91c7d2d` | (see git log) |
| `8db5ef8` | (see git log) |
| `4215799` | (see git log) |
| `8cd7e01e` | (see git log) |
| `7ce2e758` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete
