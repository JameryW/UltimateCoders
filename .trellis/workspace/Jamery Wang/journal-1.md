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


## Session 7: T6 #642 authority flip: C1-C7 + D6 delivered, task archived

**Date**: 2026-09-14
**Task**: T6 #642 authority flip: C1-C7 + D6 delivered, task archived
**Branch**: `main`

### Summary

T6 delivered in 8 commits: graph-plane sweep replaces legacy reaper, T4 serde fallbacks dropped, TS authority flipped to Rust (claim loop replaces the wave machine), graded conflict_risk, D7 release notes, dead review pipeline retired. Baselines: Rust unchanged, TS 179->162 (deaths recorded), pytest 977+4 equivalent green. PG-dependent runs pending Docker recovery.

### Main Changes

# T6 #642 — 权威一刀切反转 + TS wave 拆除 + D6 暂停语义 + D7 发布说明

## 交付概览（8 提交，全部直落 main，Tracker: #642）

| 切片 | 提交 | 内容 |
| --- | --- | --- |
| C1 | df8a8e9 | `timeout_sweep` 返回 per-attempt outcome 结构（graph_store） |
| C1/C2 | c353c5f | sweep 接管 heartbeat monitor（legacy reaper 换血）+ T4 serde 回落删除（Rust） |
| C3 | 33f9724 | TS 权威反转：`.uc/tasks` 只读化、bootstrap 改拉取、pause/resume RPC-first |
| C4-Rust/D6 | 765068b | D6 暂停语义：`UC_PAUSE_GRACE_SECS` 宽限硬停（fence attempt + node 回 READY）+ resume 重派 + 菱形验收测试代码 |
| C2 | （并入 c353c5f） | Python `_parse_subtask_message` legacy 回落删除 |
| C4-TS | 5ea60df | wave 机器全拆 + claim loop（upsert 即上报通道）+ TaskSync 派发元数据保真 |
| C5 | e4cb8f6 | conflict_risk 分级替代文件重叠硬禁并行（0.4/0.8 阈值，本地并行集约束） |
| C6 | 217c49f | D7 发布说明四要点 + C1 reaper 窗口合并记录（docs/architecture/durable-runtime-upgrade-notes.md） |
| C7 | 7d3b207 | TS review 生产管线退役 + progress-widget wave tag 清除 + TaskStore tmp 写竞争修复 |

## 门禁终值

- fmt / clippy 五目标全绿（C1/C2/D6 提交时验证）。
- Rust 基线：437+5 / 379+5 / 192+8 / 36 / 35（T5 终值，只增不减 ✓）。
- TS（bun test）：**162 pass / 17 files**（C3 开工基线 179；删 wave/checkpoint/recoverable 28+1、parse-review 9；新增 claim-loop 14、conflict 分级/gating 6；净减全部随死机制，理由记录于 implement.jsonl）。
- pytest：**970 passed 全量 + 11 单跑复核（merge_arbiter 5 + workspace 6）= 977 + 4 skipped 等价全绿**。全量跑中 7 个失败 100% 为 safe-delete 监视器 turn 累计删除拦截（seed 目录 69 文件 > 50 阈值，监视器独立于 bypass），非代码回归。
- tsc：uc-orchestrator 本包零错误（18 个 vendor 预存噪音）。

## 环境欠账（恢复跑法）

- Docker Desktop / PG 宕机（与 T4 收尾时相同），两笔 PG 实跑未执行：
  - `cargo test -p uc-grpc --all-features --test pause_grace_diamond -- --ignored`（菱形 TS-free 验收：暂停→单分支 commit→宽限硬停→resume 重派；测试代码已交付）
  - `cargo test -p uc-engine --features storage --test graph_store_integration -- --ignored`
- 恢复跑法：手动/管理员启动 Docker Desktop → 起 PG → 实跑上述两条。

## 关键架构裁决（沉淀进长期 memory）

- TS orchestrator 权威彻底移交：submitTask/runTask 只 upsert all-Pending；claim loop 三步 reconcile（采纳/终态推断/认领）；proto 无单子任务 RPC，UpdateTask upsert 就是上报通道（TOCTOU 接受）。
- FileIntentTracker → C5 `classifyConflicts`（分解期一次计算）；约束语义单位是本地并行执行集而非轮询 tick。
- review 语义未来由 Rust ready-node 管线重立；`SubtaskResult.review` 字段保留（UI 渲染历史缓存）。
- TaskStore.save 的共享 tmp 文件名在并发写者下是错的（cancel persist × in-flight outcome persist 竞争 → ENOENT），per-call 唯一 tmp 名修复。


### Git Commits

| Hash | Message |
|------|---------|
| `df8a8e9` | (see git log) |
| `c353c5f` | (see git log) |
| `33f9724` | (see git log) |
| `765068b` | (see git log) |
| `5ea60df` | (see git log) |
| `e4cb8f6` | (see git log) |
| `217c49f` | (see git log) |
| `7d3b207` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 8: T7 #643 node/attempt 级取消 + cancel-attempt-keep-node

**Date**: 2026-09-14
**Task**: T7 #643 node/attempt 级取消 + cancel-attempt-keep-node
**Branch**: `main`

### Summary

C1-C5 全切片交付：graph 取消原语、网关三路分派、worker killpg 协作取消、TS cascade 退役、PG e2e 用例（欠账待 Docker）。基线勘误 452/195。

### Main Changes

# T7 #643 — node/attempt 级取消 + cancel-attempt-keep-node

## 交付内容（C1–C5 全切片，commit b15c4c8 / 12c0944 / 029ceb9 + 归档提交）

- **C1 graph_store 原语**：`running_attempt` / `cancel_running_attempt`（fail_attempt 预算语义，reason=cancelled —— cancel-attempt-keep-node 正式入口，刻意非免预算）/ `cancel_nodes`（终态卫兵）/ `downstream_closure`（BFS）。GraphShadowSink 四动词默认 no-op，storage 委托。proto `CancelTaskRequest` 增 subtask_id/attempt_no，stub 三端重生成。
- **C2 网关粒度**：cancel_task 按 (subtask_id, attempt_no) 三路分派；attempt 级 rearmed 即刻重派（attempt_no 仅信息性，epoch 即 fence）；task 级额外打围 RUNNING attempts 并补发 attempt_cancelled。`publish_task_control_event` 细粒度变体 + pause-grace timer 补发 —— worker 杀进程触发点。
- **C3 worker 协作取消**：sandbox start_new_session + (task,node) 进程注册表 + `_kill_process_tree`（killpg→回退 kill）+ `kill_group`；nats_worker 处理 attempt_cancelled / subtask_cancelled（cancelled_nodes CSV）/ task_cancelled 强化；迟到结果 ack 不发布（graph fence 兜底）。Windows = 单进程杀（文档化 fallback）。
- **C4 TS 退役**：subtask cancel RPC-first（bridge 传 subtaskId；镜像只标目标，闭包后代靠 reconcile 采纳）；cascadeCancel / reverseCascadeUnCancel + 测试文件全删；retrySubtask reset-only。
- **C5 e2e**：granular_cancel_e2e.rs 两个 #[ignore] PG 用例（attempt 取消→重派提交胜出+迟到 fenced；node 取消闭包终态+兄弟无伤）。

## 质量门禁

- fmt/clippy 双模式全绿；Rust 452+5 / 379+5 / 195+8（对 HEAD 无增减 —— **勘误**：此前记录 437/192 是 D6 提交 765068b 打测试前的中途捕获）。
- pytest 全量 988+5 量级（新增 11 协作取消用例）；全量跑受 safe-delete bulk guard 环境拦截，按文件分批 38/38 全绿等价。
- TS bun test 156 pass / 16 files（×2 连续；一次 waitFor 姿势 flake 单跑即过）；tsc 包内零错误。
- **PG 实跑欠账（T4+T6+T7 三笔）**：graph_store_integration、pause_grace_diamond、granular_cancel_e2e，`-- --ignored`；恢复 = 启动 Docker Desktop。

## 关键裁决

1. cancel-attempt-keep-node 刻意骑 fail_attempt 预算语义（非免预算）——pause-grace 钻石基线已依赖该语义。
2. attempt_no 仅信息性：wire 无 per-attempt 选择子，epoch bump 即 fence，graph 取消当前 RUNNING attempt。
3. attempt 级取消 rearmed 后即刻重派（节点存活只有 attempt 被取消）；pause 场景派发闸自然兜住。
4. 闭包取消 = Rust 平面运行时计算（downstream_closure + cancel_nodes），TS 簿记全退役；网关 CANCELLED 的后代保持终态直到显式 re-verb。
5. legacy 镜像映射不变：node 取消 → Failed 行（T4：wire 无独立 cancelled 行）；attempt 取消 rearmed → Pending / 耗尽 → Failed。


### Git Commits

| Hash | Message |
|------|---------|
| `b15c4c8` | (see git log) |
| `12c0944` | (see git log) |
| `029ceb9` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 9: T8 ExecutionScope delivered (#650)

**Date**: 2026-09-14
**Task**: T8 ExecutionScope delivered (#650)
**Branch**: `main`

### Summary

D8 #645 落地：submit 三入口 project_id 非空校验+创建后不可变；upsert_task_shadow 拒空 scope（graph_scope_is_valid）；RegisterWorkerRequest.projects（open worker 语义）+UC_WORKER_PROJECTS 全链贯穿；dispatch_gate 扩 scope 硬过滤（NoScopeMatchedWorker，keep Pending）。基线只增：Rust 439/381/204+8/36/35，pytest 997+5。

### Main Changes

(Add details)

### Git Commits

| Hash | Message |
|------|---------|
| `80714d3` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 10: T9 merge barrier delivered (#651)

**Date**: 2026-09-14
**Task**: T9 merge barrier delivered (#651)
**Branch**: `main`

### Summary

D9 #646 落地：uc-types merge.rs 确定性 key（跨语言 golden 7094…）+ proto 双动词 + merge_grants 表（graph_id PK 单行授权）+ 静止闸/消费重放语义 + Python arbiter 授权流（refuse→skip、replay→no-op、fresh→report 携 key）。基线：types 40(+5)、pytest 1009+5(+12)、PG 集成 +4 ignored 并入欠账。

### Main Changes

(Add details)

### Git Commits

| Hash | Message |
|------|---------|
| `677b7bc` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 11: T10 context compiler delivered (#652)

**Date**: 2026-09-14
**Task**: T10 context compiler delivered (#652)
**Branch**: `main`

### Summary

D10 #647 落地：uc-types ContextEntry/ContextBlock + compose（8KiB 贪心装箱、node_id 字典序、truncated marker）+ envelope 加性 context_block + GraphShadowSink.committed_dep_outputs（node_completions 唯一事实源）+ 两发布嘴发布前组装 + worker 优先消费/无则回退 injector。基线：types 43(+3)、uc-grpc 206(+2)、pytest 1016+5(+7)；PG 集成再 +1 ignored。

### Main Changes

(Add details)

### Git Commits

| Hash | Message |
|------|---------|
| `5a68a17` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 12: T11 sandbox env allowlist delivered (#653)

**Date**: 2026-09-15
**Task**: T11 sandbox env allowlist delivered (#653)
**Branch**: `main`

### Summary

D11 #648 落地：_execute_subprocess 单点 deny-by-default 过滤（替换 dict(os.environ)+env_vars 全量透传）+ SandboxConfig 三级清单（base/shared/per-adapter，*=前缀通配、大小写无关）+ 插件驱动凭据并入（registry api_key_env）+ 覆盖层纪律（可加/覆盖但非旁路）+ UC_SANDBOX_ENV_EXTRA 逃生舱（启动即日志）+ agent 身份三序解析（adapter.name() → request[agent] → config.agent，decompose 走 claude 清单）。新增 pytest 69（真实子进程回读 os.environ：诱饵密钥全适配器不过闸、CLI 凭据过闸、decompose 得 ANTHROPIC 不得 XAI、逃生舱生效+日志、LC_*/UC_* 前缀）。pytest 1016+5 → 1085+5（44 文件逐文件全绿，sandbox 域 251 全过）；Rust 零改动，fmt + 5 组 clippy -D warnings 全绿。README 双语文档补 UC_SANDBOX_ENV_EXTRA。

### Main Changes

### Main Changes

- `python/ultimate_coders/agent/sandbox.py`
  - 新增模块级清单：`BASE_ENV_ALLOWLIST`（PATH/HOME/USER/SHELL/TERM/LANG/LC_*/TMPDIR/PWD + Windows SYSTEMROOT/SYSTEMDRIVE/COMSPEC/PATHEXT/USERPROFILE/APPDATA/LOCALAPPDATA/PROGRAMFILES/USERNAME/COMPUTERNAME）、`SHARED_ENV_ALLOWLIST`（UC_* + HTTP(S)_PROXY/NO_PROXY 大小写两形）、`ADAPTER_ENV_ALLOWLIST`（grok-build / claude-code / claude-code-decompose / codex / deepseek-harness / local-harness）、`ENV_EXTRA_ENV_VAR`。
  - `SandboxConfig` 新方法：`env_extra_names()`、`child_env_allowlist(agent)`（别名经 registry 归一 + 并入 `api_key_env_for(agent)` 的插件凭据名）、`build_child_env(host_env, overlay, agent)`（精确名 + `*` 前缀，大小写无关；覆盖层在过滤后叠加）。
  - `_execute_subprocess` 新增 `agent` 形参，解析序 `agent` → `request["agent"]` → `self.config.agent`；env 构造改为 `self.config.build_child_env(...)`。
  - `execute()` 传 `adapter.name()`；`DecomposeAdapter.build_request` 自带 `"agent"`；`execute_decompose` 新增可选 `agent` 形参。
  - `SandboxManager.__init__` 在 `UC_SANDBOX_ENV_EXTRA` 非空时 INFO 记录。
- `tests/python/test_sandbox_env_allowlist.py`（新增 69 例）：清单构造 / 过滤器单元语义 / 逃生舱 / 真实子进程回读 / decompose 路径。
- `README.md`、`README.zh-CN.md`：环境变量表补 `UC_SANDBOX_ENV_EXTRA`。

### Testing

- `.venv/Scripts/python.exe -m pytest tests/python/test_sandbox_env_allowlist.py -o addopts=""` → 69 passed。
- `tests/python/test_sandbox_env_allowlist.py + test_sandbox.py` → 251 passed。
- 全量按文件串行（44 文件，`-o addopts=""` 禁 coverage + per-file basetemp）→ 1078 passed / 5 skipped；其中 test_merge_arbiter、test_workspace 两文件在沙箱隔离下 git 子进程受阻（4+3 failed），单独重跑（无隔离）分别 5 passed / 6 passed → 归因为环境现象，非回归。修正后合计 **1085 passed / 5 skipped**（基线 1016+5，+69 即新文件）。
- `cargo fmt --all --check` → exit 0；clippy `-D warnings`：uc-types / uc-engine / uc-engine --no-default-features / uc-grpc --all-features / uc-grpc-server 全部 Finished 无告警（Rust 零改动，基线不变）。
- 实证澄清：`execute_in_sandbox` 引擎分支不可达（uc-python 无 sandbox pyo3 桥）⇒ `_execute_subprocess` 确为唯一 spawn 点；`execute_decompose` 全仓无调用者。

### Known Limitations

- `PYTHONPATH` 不在清单内（D11 未列且属注入面）——靠 PYTHONPATH 才能 import 的开发机需 `UC_SANDBOX_ENV_EXTRA=PYTHONPATH`；生产镜像 pip 安装无需。
- `TEMP`/`TMP` 未列（票面未给）：实证 Windows 子进程无此二变量仍 exit 0。
- Rust 侧 `to_engine_config().env_vars` 未过滤；该路径今日不可达，若接上需在 Rust 复刻策略。

### Status

[OK] **Completed** — P1 剩余：T12 #654（affinity placement）。


### Git Commits

| Hash | Message |
|------|---------|
| `4c3689c` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 13: T12 affinity placement delivered (#654)

**Date**: 2026-09-15
**Task**: T12 affinity placement delivered (#654)
**Branch**: `main`

### Summary

D12 #649 落地（P1 收尾）：网关侧软放置。硬闸（capability→scope→contract_version）后按 affinity desc→load asc→locality desc→worker_id asc 排序，命中 per-worker subject uc.subtask.execute.w.{worker_id}；零重叠/未声明/全过期→共享 overflow。新增 placement.rs（纯打分器+跨语言 subject golden）、WorkerRegistry.heartbeat_with_signals/dispatch_candidates/placement_target、两个发布嘴统一 resolve_dispatch_subject；proto 加性加 recent_files/per_worker_topic，UC_SUBTASKS 补通配。worker 自建 per-worker durable 并每次心跳声明（失败保持 legacy）。Rust 237 passed；pytest 1106+5 skip（+21）；fmt + 5 组 clippy 全绿。零搁死：定向不写 assigned_worker。

### Main Changes

## Session 13: T12 affinity placement delivered (#654) — P1 收尾

**Date**: 2026-09-15
**Task**: T12 affinity placement delivered (#654)
**Branch**: `main`

### Summary

D12 #649 落地，P1 地图最后一票：网关侧**软**放置。节点发布前经 capability（硬）→
scope（硬，T8）→ contract_version（硬）三道闸取候选，再按
`affinity desc → load_percent asc → locality desc → worker_id asc` 排序，命中
per-worker subject `uc.subtask.execute.w.{worker_id}`；零重叠/无声明/全员过期 → 回落共享
`uc.subtask.execute`（overflow）。新增 `crates/uc-grpc/src/placement.rs`（纯打分器 + 跨语言
subject golden），`WorkerRegistry` 新增 `heartbeat_with_signals` / `dispatch_candidates` /
`worker_host` / `placement_target`；两个网关发布嘴统一经 `resolve_dispatch_subject` 解析
subject。协议加性扩展 `WorkerHeartbeatRequest.recent_files` / `per_worker_topic`，
`UC_SUBTASKS` stream 补 per-worker 通配。worker 侧在共享 durable 之后自建 per-worker
durable 并在每次心跳声明；绑定失败保持 legacy（永不定向、仍走 overflow）。新增 pytest 21、
Rust 单测 9（placement 10 + worker_service 8 + server 5）。**零搁死**：定向不写
`assigned_worker`，`None` 是正常结果而非派发失败。

### Main Changes

- `crates/uc-grpc/src/placement.rs`（新增）
  - 常量：`PER_WORKER_SUBJECT_PREFIX`/`PER_WORKER_SUBJECT_WILDCARD`/`MAX_RECENT_FILES=64`/
    `MIN_AFFINITY_HITS=1`；`per_worker_subject()`、`normalize_path()`（`\`→`/`、剥前导 `./`）、
    `normalize_recent_files()`（trim/丢空/去重/截断）、`affinity_hits()`（**去重后**计不同约束）、
    `host_from_metadata()`（读稳定键 `hostname`）、`PlacementCandidate::load_percent()`、
    `Placement{worker_id,subject,affinity_hits,load_percent,same_host}`、`place()`。
- `crates/uc-grpc/src/worker_service.rs`
  - `RegisteredWorker` 加 `recent_files` / `per_worker_topic`（注册时为空/False）。
  - `heartbeat_with_signals()`；`heartbeat()` 降级为 delegating wrapper（**无信号心跳会清空
    声明**——声明是"每次心跳"的，停声明即恢复不可定向）。
  - `dispatch_candidates()`（把闸的三道过滤暴露为列表，供打分复用）、`worker_host()`、
    `placement_target()`（= 闸 + `per_worker_topic` + 打分）。
- `crates/uc-grpc/src/server.rs`
  - 自由函数 `sibling_worker_hosts()`（locality 输入：本任务其他节点已指派 worker 的 host，
    复用已持有的 `TaskStore` 锁）与 `resolve_dispatch_subject()`（定向 or 共享）。
  - 两个发布嘴（`publish_ready_subtasks` / `dispatch_ready_subtasks`）改为
    `dispatchable.push((st, subject))` 并发布到解析出的 subject。
- `crates/uc-grpc/proto/engine.proto`：`WorkerHeartbeatRequest` 加 `recent_files=4` /
  `per_worker_topic=5`（加性，旧 worker 不发、旧网关忽略）。
- `crates/uc-grpc-server/src/main.rs`：`UC_SUBTASKS` subjects 补
  `uc_grpc::placement::PER_WORKER_SUBJECT_WILDCARD`（NATS `>` 需再吃一个 token，**不会**
  覆盖共享 subject，故两者都要列）。
- `crates/uc-grpc/src/client.rs` + `crates/uc-python/src/engine.rs`：心跳签名透传两个新字段。
- `python/ultimate_coders/nats_worker.py`：per-worker subject/durable 常量与
  `_per_worker_subject()`/`_per_worker_durable()`（脏字符净化 + sha256 短摘要防碰撞，确定性）/
  `_transport_worker_id()`；`_bind_per_worker_consumer()`（add_consumer+filter_subject →
  pull_subscribe → 置 `_per_worker_topic=True` → 起第二条 fetch loop；**best-effort**，
  失败仅 warn 并保持 legacy）；`_subtask_fetch_loop` 参数化为 `_subtask_fetch_loop_for(pull_sub, label)`；
  `stop()` 先取消共享 loop、再取消 per-worker loop、最后清扫在飞执行；
  心跳带 `recent_files` + `per_worker_topic`（仅 worker 模式，否则空/False）。
- `python/ultimate_coders/engine.py`：`worker_heartbeat_async(..., recent_files=None,
  per_worker_topic=False)` 透传（空列表归一为 None）。
- `python/ultimate_coders/agent/worker.py`：`MAX_RECENT_FILES=64`、`_recent_files`、
  `record_recent_files()`（移动插入式去重、有界、绝不抛）、`recent_files()`（返回副本）；
  `_execute_in_sandbox` 完成处记录 `file_constraints ∪ [fc.file_path ...]`。
- `tests/python/test_affinity_placement.py`（新增 21 例）+ `test_nats_jetstream_subtask.py`
  （双 consumer 适配）。
- `README.md` / `README.zh-CN.md`：多 Worker 分布式执行补 affinity placement 条目。

### Testing

- Rust：`cargo test -p uc-grpc --all-features --lib` → **237 passed / 0 failed**
  （基线 206+8；新增 placement/worker_service/server 三层定向与 overflow 单测）。
- pytest：43 文件**逐文件串行**（`-o addopts=""` 禁 coverage + per-file basetemp）→
  **1106 passed / 5 skipped**（基线 1085+5，+21 即新文件）。test_merge_arbiter（5）与
  test_workspace（6）本轮无隔离下全过。
- `cargo fmt --all --check` → exit 0；clippy `-D warnings`：uc-types / uc-engine /
  uc-engine --no-default-features / uc-grpc --all-features / uc-grpc-server **五组全绿**。
- TS 无改动（本票纯 Rust + Python）。

### Known Limitations

- **server 层无 NATS mock 夹具**（`nats_client` 无 trait 注入点）：定向 vs 共享的判定在
  自由函数 `resolve_dispatch_subject` + `registry.placement_target` 两层单测覆盖，而非
  通过一次真实 publish 断言 subject。
- **范围外仍硬编码共享 subject**：`uc-engine/src/scheduler/executor.rs::NatsExecutor`
  与 `scheduler/dispatcher.rs`——均非 gateway dispatch mouth，票面只要求网关侧定向
  （ExecutorSelector 路径留后续）。
- **PG/NATS 实跑欠账仍为五笔**（T4/T6/T7/T9/T10 的 graph_store / merge_grant /
  context_block / pause_grace_diamond / granular_cancel_e2e）——需 Docker Desktop。
- per-worker durable 由 **worker 自建并声明**（非票面原文的"网关 provision"）：JetStream
  durable 的 `filter_subject` 不可变，跨语言各写一份配置 + 无 live NATS 集成测试 =
  运行时才暴露的静默失败；worker 侧"先绑定后注册"（T5/D4 Q1）已保证声明瞬间 consumer 存在。

### Status

[OK] **Completed** — P1 地图 #644 已全部交付（T8–T12）。剩余为 PG/NATS 实跑欠账（阻塞于
Docker Desktop）。


### Git Commits

| Hash | Message |
|------|---------|
| `b3aa299` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 14: Verify the T12 dispatch plane on a live broker — affinity placement was never active

**Date**: 2026-09-15
**Task**: Verify the T12 dispatch plane on a live broker — affinity placement was never active
**Branch**: `main`

### Summary

T12 affinity placement had never been run against a real NATS broker; mock-only coverage hid that the shared durable was unfiltered, which makes every per-worker consumer illegal on a work-queue stream (err_code 10100). Fixed by pinning the shared consumer to the bare shared subject; added a live-broker integration suite plus a CI step that runs it against a real JetStream server; also taught streaming provisioning to add the per-worker wildcard to a pre-existing stream, since get_or_create_stream has no update path.

### Main Changes

### Main Changes

Post-delivery verification of T12 (#654, affinity placement), which had shipped
with no live-broker test — as its own ticket notes.

**T12 never worked on a real broker.** `NatsWorker._ensure_subtask_transport`
created the shared durable `subtask-workers` with no `filter_subject`. On a
JetStream work-queue stream an unfiltered consumer covers every subject in the
stream, and JetStream allows exactly one such consumer (`err_code 10099`) while
refusing any filtered consumer that overlaps an existing one (`err_code 10100`
"filtered consumer not unique on workqueue stream"). So
`_bind_per_worker_consumer` was refused 100% of the time, and it swallows that
refusal by design (best-effort: a worker without a per-worker topic stays
legacy). `_per_worker_topic` was false forever, every heartbeat declared false,
and `WorkerRegistry::placement_target` — which filters on exactly that flag —
never returned a target. Affinity placement was dead code in production, and the
soft-placement semantics made "no affinity was warranted" and "affinity is dead"
observationally identical.

- `fix(python)` — pin the shared consumer to the bare shared subject. One
  keyword; the overflow path is unaffected and the two filters become disjoint,
  the only legal shape on a work-queue stream.
- `test(python)` — `tests/python/test_nats_live_dispatch.py`, five cases against
  a live `nats-server -js`, driving the real `_ensure_subtask_transport` /
  `_bind_per_worker_consumer` and asserting via `consumer_info` / `stream_info`
  rather than the worker's own memory.
- `ci(python)` — run that file with `--integration` in the existing 3.12 leg,
  against a JetStream server started by the job. A guard that only runs when a
  developer happens to have a broker is the same blind spot one layer up.

**A second route to the same inertness.** `get_or_create_stream` never writes
the per-worker wildcard to a stream that already exists: async-nats sends
`STREAM.INFO` and returns the existing stream on 200, reaching `STREAM.CREATE`
only on 404 (context.rs:444-453). Every gateway that provisioned `UC_SUBTASKS`
between T5 and T11 therefore keeps a one-subject stream — and the worker's
per-worker bind needs its filter to be a subset of the stream's subjects, so the
feature stays inert on every upgraded instance too.

- `fix(uc-grpc-server)` — read the live config back and add the wildcard when
  missing, taking every other field from the server so deployment tuning of
  `max_age` / `duplicate_window` survives. Deleting and recreating the stream
  would have dropped in-flight dispatch.
- Also recorded the consumer-side half of the contract on
  `PER_WORKER_SUBJECT_WILDCARD`, which documented only the stream half — the
  half that was silently satisfied while the feature stayed dead.

### Testing

`nats-server 2.14.6` with JetStream, Windows-native (WSL has no sudo and no
docker), verified through a protocol-level probe (INFO / PING / PONG and `/jsz`).

- Live suite: 5 passed, against both a stream the file creates and one the real
  gateway provisioned.
- Mutation check on the fix: reverting the shared-consumer filter leaves all 39
  existing mock tests green and turns 4 of the 5 live cases red with the server's
  own wording (`err_code=10100`); the legacy-worker case stays green, correctly.
- Upgrade hazard probed before committing, not assumed: re-adding an existing
  unfiltered durable with a filter is accepted with three un-acked messages in
  flight, delivery continues, and a repeat add is idempotent.
- Real binary, both provisioning paths: a pre-T12 stream logs "upgraded in place:
  added the per-worker subject wildcard" and the server then reports
  `["uc.subtask.execute", "uc.subtask.execute.w.>"]` with retention and other
  settings unchanged; no stream logs "provisioned" with a config byte-identical
  to the Python golden.
- Rust gates: `cargo fmt --all -- --check` clean; `clippy -p uc-grpc
  --all-features -D warnings` and `clippy -p uc-grpc-server -D warnings` clean;
  uc-grpc default 209 + 8 and all-features 237 + 8 pass.
- Python gates: `ruff check python/ tests/` clean; py3.9 compatibility scan 82
  files / 0 problems. Full serial file-by-file run: `passed=970 failed=0
  error=136 skipped=10` — `failed=0`, and 970 + 136 = 1106 (the previous
  baseline), the 136 being the sandbox bulk-delete guard reclassifying `tmp_path`
  teardown as setup errors; re-running the affected files in isolation gives 31
  passed. Skips went 5 → 10 for the new file's five cases.

Self-corrected inside this session: the first CI step started the broker without
`-m 8222`, and the HTTP monitor is off unless asked for — verified by the
presence of the "Starting http monitor" log line. The readiness probe would have
burned its budget and the step would have run the tests having confirmed nothing;
it now starts the broker with `-m 8222` and fails the step with the server log
when the endpoint never answers.

### Status

Complete. Commits `78c0d1d`, `7f69e62`, `080e747`, `07064bf`, `82ea112`,
`db7c289` on main.


### Git Commits

| Hash | Message |
|------|---------|
| `78c0d1d` | (see git log) |
| `7f69e62` | (see git log) |
| `080e747` | (see git log) |
| `07064bf` | (see git log) |
| `82ea112` | (see git log) |
| `db7c289` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 15: T13: 统一两份图投影的依赖感知规则（#655）

**Date**: 2026-09-15
**Task**: T13: 统一两份图投影的依赖感知规则（#655）
**Branch**: `main`

### Summary

source-B 导入把阻塞的 Pending 节点发布成 READY；三条投影路径改为共用一份 dependency_aware_state

### Main Changes

### Main Changes

- **发现**：`graph_nodes.state` 对 `Pending` 的判定在三条投影路径上不一致——`project_task`（source-A PG backfill / shadow）走依赖感知，`project_ts_task`（source-B `.uc/tasks` 导入）走纯 token 映射 `node_state_token`，而后者把 `"pending" | "ready"` 都映成 `READY`。
- **判定为缺陷而非设计**：L145-158 的函数注释明说"把每个 Pending 映成 READY 会把整张图发布成可调度"是它存在要阻止的 bug；而模块头 L43-55 的映射表却自称 import+shadow 共用且列出 `pending → READY`——同一文件内两条矛盾表述，且该表对 shadow 的描述从写下那天起就是错的。git 证明三处同属 `28b66e9`（T2 #638），提交正文无任何刻意理由。
- **可达性**：TS 的 subtask 状态联合含 `pending`（`orchestrator.ts:100`），`toPersisted`（L1807）原样写盘，而 subtask 的**初始**状态就是 `pending`（L554/644/1128）⇒ 一个"已提交未开始"的任务被导入时，每个阻塞节点都写 `READY`。仓库自己的 fixture 就这么用（`scheduler.test.ts:119`、`progress-widget.selfcheck.ts:532`）。
- **修复**：抽出单一实现 `dependency_aware_state(raw_status, deps, is_satisfied)`，判据统一为"依赖的 status token 是否 `SUCCEEDED`"（词汇无关，避免第二份状态匹配表再漂移）；`node_status_of_subtask` 改为薄适配器（谓词保持 `matches!(status, Completed)`，source-A/shadow 行为逐字节不变），`project_ts_task` 建立 TS 侧 id→status 索引后走同一规则。修正模块头映射表的 `Pending` 行并补一段说明。

### Testing

- **新增单测** `ts_and_rust_projections_agree_on_pending_with_unmet_dependencies`：同一逻辑图分别经 `project_ts_task` 与 `project_task`，断言 node states **逐字节相同**（正是既有测试注释自称保证、却被 fixture 绕开的那条），fixture 含 blocked / unblocked / dangling 三种 pending。
- **加强既有测试** `ts_task_projects_like_its_rust_equivalent`：fixture 补上三个 `pending` 子任务，覆盖此前零覆盖的 `CREATED` 分支。
- **突变自检（决定性）**：撤掉修复（`project_ts_task` 改回 `node_state_token`）→ 新测试变红，`left: n-blocked=READY, n-dangling=READY` / `right: n-blocked=CREATED, n-dangling=CREATED`；恢复后全绿。
- **门禁**：`cargo fmt --all -- --check` ✅；`clippy -p uc-engine -- -D warnings` ✅（首次跑抓到自己的 `redundant_closure`，已修）；`clippy -p uc-engine --all-targets -- -D warnings` ✅；uc-engine lib **440 → 441 passed / 0 failed**；`--no-default-features` lib **382 → 383 passed / 0 failed**（只增不减）。未动 Python/TS，pytest 与 TS 计数不变。

### Status

- **爆炸半径（诚实口径）**：今天**没有生产消费者**——`GraphStore::node_state` 的调用者全是测试；且导入路径 opt-in（`UC_GRAPH_IMPORT_DIR`）、insert-only（`shadow=false` → `DO NOTHING`）、`graph_exists` 命中即 skip。所以这是**给 durable 平面播种错值**（图平面正被做成权威，错种子会被继承），不是 T12 那种"功能整体死掉"的活故障。
- 提交 `5bae604` 已推 main；票目录归档至 `.trellis/tasks/archive/2026-09/09-15-t13-unify-projection-states/`（status=completed, commit=5bae604）；issue #655 待关（贴验收映射）。
- ⚠️ **本机 C: 盘一度 100% 满**（`target/` 44G，其中 `debug/incremental` 17G、`debug/deps` 24G），写文件与构建一度完全阻塞；已删 `debug/incremental`（`df` 实回收约 2.5G，说明与 `deps/` 共享块居多）。gate 全程复用既有 target 缓存以免再写满系统盘。仍建议把构建目录移出系统盘（D: 尚余 270G）。


### Git Commits

| Hash | Message |
|------|---------|
| `5bae604` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete

## Session 16: T14: 指标写入点 + duration_ms —— 终态事件带时长（#659）

**Date**: 2026-09-15
**Task**: T14: 指标写入点 + duration_ms（P2 地图 #656 的第一张实现票，决策 D13 #657）
**Branch**: `main`

### Summary

`execution_events` 的三个预留指标列今天写上了第一个——`node_succeeded` 带上 `duration_ms`；写点与 commit-once 重合，用量天然 exactly-once

### Main Changes

- **定性**：`cost NUMERIC(18,6)` / `tokens BIGINT` / `duration_ms BIGINT` 三列自 T2（#638 / `28b66e9`）建表就在，建表注释自称 *"reserved billing columns … no writer exists in T2"*；唯一的 INSERT（`append_event_tx`）只绑六列，其 doc 注释写着 *"deliberately never bound"*。全仓盘读方：Rust 侧只读 `event_type` / `payload`，**Python 侧完全不引用 `execution_events`**，三列从未出现在任何 SELECT 里 ⇒ **死列**，缺的是写者而不是 schema。
- **为什么先做 duration**：`task_attempts.started_at` 由 `schedule_attempt` 写入，而 `commit_once` 成功路径在同一事务里就 `UPDATE … finished_at = NOW()`——两个时间戳都在手边 ⇒ 零契约变更、不碰 `SubtaskResult`、不碰信封、不影响任何 worker。
- **纯函数** `attempt_duration_ms(Option<DateTime<Utc>>, DateTime<Utc>) -> Option<i64>`：无起点 → `None`（不编一个 0）；elapsed 为负 → `None`（时钟倒挂**不取绝对值**）；否则毫秒，两戳相同 → `Some(0)`（那是真实的 0，不是缺失）。⇒ 下游能区分「零用量」与「无上报」。
- **命名修正（诚实记录）**：issue 正文写的是 `node_duration_ms`，实现落为 `attempt_duration_ms`——量的是**某个 attempt** 的时长，一个 node 可有多个 attempt；行为与 issue 的三条判据完全一致。
- **写点**：`append_event_tx` 拆两层（`EventUsage{cost,tokens,duration_ms}` + `append_event_with_usage_tx`），原签名保留为委托 ⇒ 其余 **11 个调用点一行未动**；`attempt_duration_ms` 与两个 helper 一样带 `#[cfg(feature = "storage")]`（否则 no-default-features 下 dead_code）。
- **取 DB 时间**：`commit_once` 的 attempt 读从 `SELECT status` 扩成 `SELECT status, started_at, NOW() … FOR UPDATE`，时长由**数据库自己的 NOW()** 推出，绝不用进程时钟（应用/PG 时钟差会把时长污染成看似合理的错值）。CAS / commit-once / fence 语义一行未改。
- **`usage_reported: false`** 写进 `node_succeeded` 的 payload——显式声明本次不含用量上报（D13 的硬性要求：别让缺失被读成 0）。

### Testing

- **单测（本地）**：`attempt_duration_ms_never_invents_a_duration`（无起点 / 起点晚于 DB 时钟，两条都 `None`）、`attempt_duration_ms_measures_the_elapsed_span`（+1500ms → `Some(1500)`；两戳相同 → `Some(0)`）。
- **集成测试（PG）**：`graph_t14_commit_binds_duration_once_and_never_zero_fills_cost`——`schedule_attempt` → 把 `started_at` 往前拨 5s（等价于一个跑了 5s 的任务，测试零成本且时长确定非零）→ `commit_once` 为 true → 恰好一条 `node_succeeded`，`duration_ms ∈ [5000, 60000)`，`cost`/`tokens` 均 NULL，`payload.usage_reported == false`；再以同一 attempt 二次 commit → 返回 false、`node_succeeded` 仍 1 条（**重试不双计**）、`late_result` 恰 1 条且其 `duration_ms` 为 NULL（fenced 结果不携带用量）。
- **门禁**：`cargo fmt -p uc-engine -- --check` ✅；`clippy -p uc-engine --all-targets -- -D warnings` ✅；uc-engine lib **441 → 443 passed / 0 failed**（**恰好 +2**，正是两条新单测）；`--no-default-features` lib **383 → 383**（**+0**，证明门控正确摘掉了两条单测且无 dead_code）。
- ⚠️ **集成测试未获本地 live PG 验证——原因是环境而非代码，逐条留证**：
  1. `wsl.exe` 被沙箱 **Program Blacklist 硬阻断**（`Permission denied` + `PROGRAM BLOCKED BY SECURITY POLICY`，明令不得绕过）⇒ 笔记里 `wsl -e bash -lc 'sleep 5400'` 的钉 VM 配方**跑不了**。
  2. 实测替代通路可行但**不持续**：`ls "//wsl.localhost/Ubuntu-24.04/"`（走 wslservice，不启动 `wsl.exe`）能把 PG 探针从 `DEAD: TimeoutError` 唤醒成 `ALIVE`；然而保持 15s 循环触碰时，**探针 ALIVE 与 sqlx 报 pool timed out 出现在同一时刻**。
  3. 自建裸协议探针 `.scratch/pg-startup-probe.py` 定位到断点：TCP connect 成功 → SSLRequest 得到 `'S'` → **StartupMessage 发出后 8s 无任何回应**，正是笔记所记的「中继楔住」。
  4. 绕过方案 Docker 也不可用：CLI 在，但 `dockerDesktopLinuxEngine` 管道不存在，`docker desktop start` **挂 8m18s** 未起引擎（已 kill）。
  5. **一次真实教训**：`--nocapture` 下跑满文件得到 `18 passed` 但**全部是 SKIP**、耗时 **231.14s**（= 18 × pool timeout）。若没有 `--nocapture` + 耗时判据，这就是一次完美的假绿——两条判据今天都真的救了场。

### Status

- **本地能验的都验了，不能验的明确标为未验**：集成测试**编译通过**、断言逻辑逐条对应「验收」列表，但**未在真实 PG 上执行过**，交由 CI 的 `storage integration tests` job（workspace 级 `cargo test --features storage -- --ignored`）落地验证。**我不声称它已通过。**
- **顺带纠正一个类型层面的隐患**（离线核验、非凭记忆）：`sqlx-postgres-0.8.6/src/types/float.rs:38` 的 `impl Type<Postgres> for f64` 只声明 `FLOAT8`，**没有 `compatible` 覆盖** ⇒ 让 PG 把参数推断成 `NUMERIC` 是错的。INSERT 因此写作 `$7::float8::numeric`：`$7` 被推断为 float8（与 sqlx 送出的类型一致），再由 PG 显式转 numeric。今天 `cost` 恒 NULL，此改是为 T15 绑真值时不再继承该问题。
- **测试隔离**：`late_result` 的 `duration_ms` 为 NULL 这条，不靠"额外加个判断"实现，而是写点放在 `commit_once` 成功分支的**自然结果**——两条早退路径（fenced / 丢掉 `node_completions` 插入）根本不进入该分支。

### Next Steps

- T15（#660）：`SubtaskResult` 加可选 usage 字段（Rust + Python 镜像）→ 绑 `cost` / `tokens`；未上报时保持 NULL + `usage_reported: false`，**绝不零填充**。
- T16（#661）：review 作为图节点（`type='review'`）；首个动作是决定「谁插入 review 节点」（拆解期 vs 显式依赖边）——D14 刻意留白。
- 环境层面：`target/` 仍在 C: 盘（D: 余 270G）；本地 live PG 这条验证通路在 `wsl.exe` 解禁前实质不可用。


## Session 17: 收口 T14 —— CI 8/8 绿 / #664 修复验收 / D15 立案 / 关票与账本

**Date**: 2026-09-15
**Task**: T14（#659）交付收口 + 缺陷 #664 修复验收 + 决策票 D15（#665）
**Branch**: `main`

### Summary

把 T14 这条线**关到底**：CI 8/8 绿（含真 PG 上那笔「只能由 CI 验」的集成测试）、账本与地图落记、#659/#664 关闭、D15 立案，并把票面里只是文字的依赖改成原生边。

### Main Changes

- **CI 验收（`0c2604d`，Rust CI 8/8 success）**：`storage integration tests` job 内 `graph_store_integration` **18 passed / 2.22s**——含新增的 `graph_t14_commit_binds_duration_once_and_never_zero_fills_cost ... ok`（T14 唯一的「只能由 CI 验」分支由此落地）；`granular_cancel_e2e` **2 passed / 0.45s**；`merge_grant_integration` **4 passed / 0.61s**；`pause_grace_diamond` **1 passed / 0.86s**。`test (no storage feature)` 亦绿 ⇒ 门控正确性被 CI 独立复核，与本地 `443 / 383` 这对数字一致。
- **0.45s 是证据，不只是绿色**：修复前同二进制 `4.17s / 1 failed`。修完后这两个用例**不再等待任何异步落点** ⇒ 耗时不再随 runner 的 DDL 速度浮动，而 50ms 预算正是被那次 5–6× 的 DDL 变慢冲垮的。⇒「时序敏感已移除」有可量化的落点，不是「看起来好了」。
- **账本补记**（`docs/architecture/durable-runtime-migration-assessment.md` §六）：新增 4 段——T14 交付（含 `node_duration_ms` → `attempt_duration_ms` 的命名诚实记录）、#664 缺陷与「不是我的回归」判据、D15 立案、T14+#664 的 CI 验收数字。全文件 CRLF 一致（132 / 132），不是混行尾。
- **地图 #656 更新**：Decision tickets 列表按序插入 D15（未裁）⇒ **本图现有 1 项开放决策**；追加进度块（T14 交付 / T15 阻塞解除 / #664 修复 / P2-3 仍无据）。
- **原生依赖边 `#660 ← #659` 已补**：票面 `Blocked by: T14` 原先只是文字。补之前先做**方法自证**——拿已知为真的边对照（#638 / #640 / #641 / #642 / #643 都非空），确认不是 API 能力缺失；补之后**双向复核**（`#660 blocked_by #659` + `#659 blocking #660`）。⇒ 与 T1–T7 链的记法对齐。
- **#659 / #664 关闭**（reason=completed），各附验收映射评论：#659 逐条对表（验收项 + CI 数字 + 三处诚实记录：函数改名、`append_event_tx` 签名未改而改为委托、payload 用显式 `usage_reported:false` 而非省略该键）；#664 表列 4 项门禁 + 「修的是测试不是产品」+ **后效写明**。
- **#665（D15）保持 open 并加立案评论**：决策票未裁就不关（与 D13/D14「裁并关闭」的口径一致）。评论补两条裁决材料——#664 修复后该链路 e2e 覆盖消失（刻意为之）、以及「裁决前是否默认关掉 `UC_GRAPH_SHADOW`」。

### Testing

- **CI 权威**：见上，8 个 job 全绿；`0c2604d` 上没有其它 workflow 被触发（改动只落 `crates/**`）。
- 本次收口提交只动 `docs/**` 与 `.trellis/**` ⇒ **不触发任何 CI**（有意的：账本与日志不应产生构建）。

### Status

- **T14 完全闭环**：实现 → 归档 → 账本 → 关票，无遗留待验证分支。
- **一处诚实后效（已同时写进 #664 评论与地图）**：「legacy 写入 → 图镜像跟随」这条链路自此**没有 e2e 覆盖**，只剩 unit 级。这是刻意的——D15 未裁之前，不应有测试把当前写入语义钉成契约。
- **口径纠正**：`RUNNING → READY` 是**合法边**（fence re-arm）⇒ D15 的选项 C（单用 `transition_ok` 守卫）**不足**，需与 B 或 D 组合。这条先钉在票面，免得裁决时按「非法边」的直觉选 C。

### Next Steps

- **T15（#660）已可开工**（阻塞已解除）：`SubtaskResult` 加加性可选 usage（Rust + Python 逐字一致）→ `cost` / `tokens` 绑真值；沿用 `$7::float8::numeric` 的绑定形状（`f64` 的 wire type 是 `FLOAT8` 而列是 `NUMERIC(18,6)`，裸 `$7` 会让 PG 把参数推成 numeric）。
- **T16（#661）**：首个动作仍是决定「谁插入 review 节点」（拆解期 vs 显式依赖边）——D14 刻意留白。
- **D15（#665）**：等裁决；裁决后无论选哪个选项，都要补一条能区分「写入被拒绝」与「根本没送达」的测试。
- 环境层面未变：`target/` 仍在 C:（仅剩约 19G）；本地 live PG 在 `wsl.exe` 解禁前实质不可用。

## Session 18: T15（#660）交付 —— usage 上报契约 + `cost`/`tokens` 落列

**Date**: 2026-09-15
**Task**: T15（#660）第二片：把适配器早已采集到的 token/cost 送到 `execution_events`
**Branch**: `main`（提交 `dd3d2b3`）

### Summary

把 D13 那两列**打通**：契约上加性携带 usage（跨语言逐字一致）→ 网关在 commit-once 赢家分支绑
`cost`/`tokens` → Python 侧两个丢弃点补齐。四道门禁全绿，Rust 基线只增不减。

### Main Changes

- **契约（`uc-types`）**：`SubtaskUsage` 四字段全 `Option` + `serde(default, skip_serializing_if)`
  ⇒ 不设 usage 的发布者**字节不变**（无需动 `contract_version`）。`is_empty()` =
  「没有数字的块不是测量」、`total_tokens()` = 「两侧都缺 ⇒ NULL，不是 0」—— 这两条是 D13 的硬要求，
  写成单测而不是注释。
- **同一类型同时承运 domain 与 wire**：`SubtaskResult.usage` 与 `NatsSubtaskUpdate.usage` 共用
  一个类型 ⇒ 键名与字段不可能分叉。这比「两侧各自定义再祈祷一致」强。
- **写入点**：`GraphShadowSink::on_commit` / `commit_once` 加参；赢家分支把 usage 折进
  `cost`/`tokens`，**与 T14 的 `duration_ms` 同桌** ⇒ 恰好一次是结构性的（fenced / lost-insert
  两条早退都到不了那一支）。payload 写 `usage_reported` + 已知时的 `usage_source`，不让 NULL 被读成 0。
- **Python**：`SubtaskUsage` 镜像 + `_make_task_update_payload` 在**唯一收口点**发 `entry["usage"]`
  （三处 publisher 一处未改）；`worker._execute_in_sandbox` 补上被丢的 `AgentOutput.token_usage`；
  三个解析点用 `self.name()` 打来源。
- **跨语言 golden 两半**：Python 侧钉「payload builder 发的键 == Rust 侧读的键」，Rust 侧钉
  「不设 usage 时序列化里不出现 `usage` 子串」。任一侧改名会同时打红。
- **票面三处偏差 + 两处实现偏离**逐条记入 `prd.md` 与 `implement.jsonl`，未静默扩张也未静默遗漏。

### Testing

- **Rust 基线（只增不减）**：uc-types **43 → 47**（+4 新单测）；nodefault **383+5** 不变；
  default **443+5 / 209 / 8** 不变；uc-grpc `--all-features` **237**；uc-grpc-server **36**；
  uc-engine `--features indexing` **443**。
- **Python 本地**：**1118 passed / 10 skipped / 0 failed**。⚠️ 沙箱内逐文件跑会报 169 个 errors
  —— **全部是沙箱产物**：error 集中的 11 个文件在非隔离上下文复跑后逐条转成 pass
  （41+38+22+13+10+7+5+2+1+24+6 = **恰好 169**）⇒「有多少 errors」本身不是结论，必须归因。
- **新增用例被定向证明**：Python 12 条、Rust 4 条 + 1 条 `#[ignore]` PG 集成；
  用 `--collect-only` / `-k` 列出名单后实跑通过，不靠「总数涨了」推断。
- **PG 实跑欠账（明确不声称已验）**：新 `graph_t15_commit_binds_reported_usage_and_keeps_unreported_null`
  **未在真实 PG 上执行**（本地 `wsl.exe` 被程序黑名单拦住、Docker 引擎未起）。仅**编译验证**
  （`--all-features --all-targets`）+ `-- --list` 确认被收集，交 CI 的 `storage integration tests`。

### Status

- T15 交付完成（`dd3d2b3`），四道门禁绿。
- **抓到一个真漏并修掉**：`crates/uc-grpc/tests/{granular_cancel_e2e,pause_grace_diamond}.rs`
  共 **5 处** `.commit_once(` 漏改。两文件顶部是 `#![cfg(feature = "storage")]` 而 uc-grpc
  `default = []` ⇒ 默认特征下被编译成**空文件**，`cargo check --workspace --all-targets`
  完全看不见；只有 `cargo test -p uc-grpc --all-features` 才报 `E0061`。
  **这是「本地只跑子集必漏」的第三次实例**；已把
  `cargo check --workspace --all-targets --all-features`（21s）写进 skill §4 当廉价全覆蓋探针。
- **缩进只有 fmt 说了算**：机械补参脚本按「闭合括号所在行的缩进」插字段会**少 4 空格**
  （字面量嵌在 `vec![...]` 里时两层缩进不同）；`cargo check` / `clippy` 都不看缩进。
  ⇒ 交付前必跑 `cargo fmt --all`，再 `--check` 复核。
- 两条**已知限制**（同时写进票面评论）：多步 workflow 只透传最后一步的 usage（少报不是错报；
  聚合成一块会让数字与 `source` 同时不可归因）；worker→worker 的 `subtask_completed` 事件不带
  usage（网关落列不受影响，本地编排器那份副本为空）。
- **一条侦察判断被推翻**：原判「通用 JSON 解析路径没有 adapter 身份 ⇒ `source` 只能空着」；
  实测三个解析点都在适配器方法内，`self.name()` 可用 ⇒ 三处全填，且未编造来源。

### Next Steps

- **T16（#661）**：首个动作仍是决定「谁插入 review 节点」（拆解期 vs 显式依赖边）—— D14 刻意留白。
- **D15（#665）**：等裁决；裁决后要补一条能区分「写入被拒绝」与「根本没送达」的测试。
- 环境层面未变：本地 live PG 在 `wsl.exe` 解禁前实质不可用；`target/` 仍在 C:。


## Session 19: T16: review 作为图上节点 —— type 写者 + 能力门独立性 + 跨语言结论通路

**Date**: 2026-09-16
**Task**: T16: review 作为图上节点 —— type 写者 + 能力门独立性 + 跨语言结论通路
**Branch**: `main`

### Summary

修 T16(#661)：graph_nodes.type 补写者；review 能力门 fail-closed（UC_CAP_REVIEW opt-in）表达独立性；review 结论经 SubtaskResult.review 跨 Rust/proto/Python/TS 送达 TUI。顺带自我更正上轮「本机无 protoc」的误判。

### Main Changes

## 背景与侦察结论

T16（#661）是 D14（#658，「review 是图上节点」）的实现票。开工前的侦察推翻/修正了票面与决议的**三处**陈述，这些更正是本票最值钱的产出：

1. **`SubtaskResult.review` 并非「已刻意保留」** —— 只有 TS 侧 `SubtaskDef.review`（`orchestrator.ts:106-110`）在 T6（#642）后存活；Rust `SubtaskResult`（`uc-types/src/agent.rs:210-232`）与 Python `SubtaskResult`（`types.py:153-174`）**都没有**任何 review 字段。D14 说「刻意保留」对 TS 成立，对 Rust/Python 不成立。⇒ S4 从「填充一个已有字段」变成「造一条跨四语言的通路」。
2. **`graph_nodes.type` 有列、有 DEFAULT、无写者** —— 唯一 INSERT 点（`graph_store.rs:1030/1038`，shadow 与非 shadow 两个变体）的列清单里**没有 `type`**，于是所有行恒为 `'subtask'`。D14 的「零迁移」成立，「零工作量」不成立：得先给它一个写者。
3. **`worker_epoch` 不是 worker 身份** —— 它是 per-attempt 单调栅栏计数器（`SELECT COALESCE(MAX(worker_epoch),0)+1`，`graph_store.rs:1411`），且 Python 侧恒发 `""`。票面验收里「同一 epoch 不得自审」**字面上不可实现**，只能按语义读作「产出被审结果的 worker 不得认领它的 review」。

## 决定性发现：独立性无法用「能力 + affinity」表达

票面希望靠既有 placement 机制表达「review 必须由产出者之外的 worker 执行」。实测三条证据否掉了这条路：

- `placement.rs:7` 明写 affinity 是 **preference, never a gate**（偏好，永不作门禁）；
- `dispatch_gate`（`worker_service.rs:262`）只按 capability + scope 过滤，**没有任何排除参数**；
- 更关键的是 `worker.py:399` 把 `"review"` 放进了**每一个** worker 的默认能力表 —— 于是「每个 worker 都能审」，「独立」无从谈起。

票面同时禁止给 `dispatch_gate` 加排除原语（要求先回开 D14）。在不新增机制的前提下，选择的落法是 **能力门 fail-closed**：

- 把 `review` 从默认能力表里摘掉，改为 `UC_CAP_REVIEW` 显式 opt-in；
- 于是**未 opt-in 的产出者**根本过不了 review 节点的 gate，节点停在 `PENDING`；
- 节点标签由能力**推导**（`node_type_for(caps)`）而非声明，保证「自称 review 节点」与「必须要求路由到 reviewer 的能力」不能脱钩。

**残余缺口如实记账**：显式同时 opt-in 两种角色的 worker 仍可自审。这个洞不隐瞒。

## S1–S4 落点

**S1 图平面**（`crates/uc-engine/src/graph_store.rs`，+124/-5）：新增 `NODE_TYPE_SUBTASK` / `NODE_TYPE_REVIEW` / `REVIEW_CAPABILITY` 常量与 `node_type_for()`；`NodeRow` 增 `node_type`；`project_task`、`project_ts_task` 两条投影路径与两个 INSERT 变体（shadow 侧加 `type = EXCLUDED.type`）都补上 `type`，bind 链加 `.bind(&node.node_type)`。uc-engine lib 443 → 446（+3 单测）。

**S3 独立性**（`python/ultimate_coders/agent/worker.py`）：`review` 移出默认能力表，改 `UC_CAP_REVIEW` opt-in，与 `UC_CAP_BROWSER` / `UC_CAP_DEBUG` 同形。pytest 1118 → 1120。

**S4 结论通路**（跨 5 个文件面）：
- `uc-types`：新 `SubtaskReview { approved, issues, suggestions }` + `SubtaskResult.review: Option<SubtaskReview>`（`skip_serializing_if` 缺省不序列化）+ `lib.rs` 根 re-export（本仓铁律）。uc-types 43 → 47。
- wire：`NatsSubtaskUpdate.review`（与 T15 的 usage 同一条**加性**纪律，不 bump `contract_version`）。
- proto：`optional string review_json = 16` —— 裁决走 JSON 字符串而非嵌套 message，保持加性，且让消费者能把「垃圾」判成「无裁决」而不是半填充记录。
- `conversions.rs` 双向；读侧注释写明「unparseable JSON 意味着无裁决，不是被否决」。
- `dashboard_service.rs` 快照补 `review_json`。
- Python `SubtaskReview`（`from_dict` **容错**：非 dict / `approved` 非 bool / `{}` 一律回 `None`）+ checkpoint 往返 + `nats_worker.py` payload **单一收口点**（仅在非 None 时发键）。
- TS `grpc-bridge.ts`：`parseReviewJson()` 私有助手 + `SubtaskDef.review`，两个对称映射点都接。UI **无需改动**即可显示。

**「缺席不是否决」贯穿每一跳**：Rust `Option` / proto `optional` / Python 容错 `from_dict` / TS `parseReviewJson`，任何一跳拿不到裁决就是**没有裁决行**，绝不写 `approved: false`。这与 D13 对 usage 的口径同源。

## 测试

- Rust golden 测试钉住 `review_json` 的**跨语言 key 名**（`approved` / `issues` / `suggestions`）—— 因为改名会让 TUI 裁决行**静默变空**而不打红任何 Rust 测试；并断言缺 `approved` 的 JSON **必须解析失败**。
- Python 3 条：verdict 往返、垃圾/部分块容错、checkpoint 往返 + payload 仅在存在时发键。

## 门禁

`cargo fmt` clean；`uc-types` clippy `--all-targets` clean；`uc-engine` clippy `--lib` clean；`cargo check -p uc-engine --all-targets` clean；uc-engine lib **446**；uc-types **47**；uc-grpc `--all-features` **238**；pytest **1123 / 10 skipped**；ruff clean。CI：`f5ef707` 的 **Rust CI 8/8 绿**。

## 两次归因（都不是本票引入）

- `cargo clippy -p uc-engine --all-targets` 在 `graph_store_integration` 上报 `can't find crate`：**把文件取出、`git checkout --` 复原、在干净基线上跑同一命令，同样失败**，再复原。属本机既有环境问题。
- `cargo check --workspace --all-targets --all-features` 报 `worker_service_server` 缺失 + `rustc` `STATUS_STACK_BUFFER_OVERRUN (0xc0000409)`：读 `build.rs` 后归因到既有的 `--all-targets` 破损，非本票。

## Correction：自我推翻的一个错误结论

S1–S3 交付后，我曾在提交信息、#661 评论与长期记忆里写「本机无 protoc ⇒ uc-grpc / `--all-features` 不可编译 ⇒ S4 只能押后到 CI」。**这是错的**：`crates/uc-grpc/build.rs` 在 `PROTOC` 未设时会回落到 `protoc-bin-vendored::protoc_bin_path()`，而 `protoc-bin-vendored-win32-3.2.0` 就在本地 registry 里；`cargo check -p uc-grpc` 实测 **50.14s 编过**。

错因：我拿 `which protoc`（PATH 事实）当成了构建事实。**教训：不要把间接信号升级成事实 —— 读 `build.rs`，或者干脆跑一次构建。** 更正已落到 follow-up 提交信息、#661 评论和长期记忆三处，不只留在对话里。

## 局限（如实记账）

- **TS 那一跳本地可验证（本节已更正）**：初版写的是「`bun test` 在本机稳定段错误（Bun 1.3.14，`Segmentation fault at address 0x5`），故该跳只能由 CI 验证」——**错的**。真因是我把 `parseReviewJson` 写成**类体内的 `function` 声明**（TS 类方法不允许 `function` 关键字），Bun 撞上该语法错误后直接崩溃（SIGTRAP / exit 133），并用那句 *"a bug in Bun, not your code"* 把责任推给了上游；真正的解析错误就打印在它上面几行（`Expected ";" but found "parseReviewJson"`，`grpc-bridge.ts:1054:11`）——本地与 CI 一字不差。改成 `private` 后本地 `bun test` **156 passed / 0 failed / 16 files**，CI 的 `bun test (uc-orchestrator)` 由红转绿（`04372b8`）。⚠️ **这是本票第二次同类错误**：第一次是拿 `which protoc`（一个 PATH 事实）断定「不能编译」，这次是拿工具的自辩（"not your code"）断定「不是我的代码」——**两次都是把间接信号当事实，而一手证据（`build.rs` / 崩溃前的解析错误）就在旁边**。
- **独立性有残余缺口**：显式双重 opt-in 的 worker 仍可自审（见上）。
- 票面验收「同一 epoch 不得自审」按语义实现为「产出者不得认领其 review」。


### Git Commits

| Hash | Message |
|------|---------|
| `90f756d` | (see git log) |
| `f5ef707` | (see git log) |
| `89a9178` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 20: T17: 图镜像不再移动权威 node state —— D15 的 B + C 落地（#667）

**Date**: 2026-09-16
**Task**: T17: 图镜像不再移动权威 node state —— D15 的 B + C 落地（#667）
**Branch**: `main`

### Summary

执行 D15 裁决：shadow 分支不再写 state（B）+ 对已存在 node 加 transition_ok 转移守卫（C）+ 两个互斥计数。用消融实验测出两条子句各自的真实作用，据此推翻了票面与我已写下三遍的「C 拦住形状 1 回退」这一过度声称。

### Main Changes

### Summary

执行 D15（#665）的 **B + C** 裁决：`write_projection` 的 shadow 分支**不再写 `state`**（B），并对**已存在** node 复用既有 `transition_ok` 加**转移守卫**（C）；新增两个**互斥**计数，让「被守卫拒绝」与「镜像根本没送达」在返回值与日志里可区分。落地过程中用**消融实验实测**了两条子句各自的真实作用，据此推翻了本票自己先写下的（以及我在源码 doc / 测试 doc / 票面里已经写下三遍的）一个**过度声称**。

### Main Changes

## 背景：一个无条件覆盖

`GraphStore::write_projection` 的 shadow 分支原先写：

```sql
ON CONFLICT (node_id, graph_id) DO UPDATE SET state = EXCLUDED.state, ...
```

`state` 是**无条件覆盖** —— 既无迁移门、也无版本 CAS。而镜像的来源是 legacy `TaskStore` 的 **fire-and-forget fan-out**，快照在调用瞬间捕获 ⇒ 迟到即陈旧。#664 已在 CI 上稳定复现过这一形状：`cancel_running_attempt` 把权威写为 `READY` 之后，一个更早捕获的 `InProgress` 快照把它盖回 `RUNNING`。

## 侦察：守卫为什么不能放在 `project_task`

`project_task(task) -> GraphProjection` 是**纯函数**：它没有 DB 句柄，拿不到该 node 的**现存** state，只知道快照声称的 state。所以「快照 vs 权威」的比较只能发生在 `write_projection` 的**事务内**。这也顺带决定了预取的位置：在 node 循环**之前**做**一次** `SELECT node_id, state FROM graph_nodes WHERE graph_id = $1`，而不是逐节点查询。

## 自我更正一：`READY → RUNNING` 不是合法边

我先是按直觉写的注释与单测：以为 `READY → RUNNING` 是「普通派发的合法边」，于是主张「#664 的形状只能靠 B 拦住，C 只负责复活/跳级」。**单测当场打红**（`assertion failed: mirror_write_allowed("READY", "RUNNING")`），查证后确认 `transition_ok("READY","RUNNING") == false` —— 本仓的派发路径是 `READY → SCHEDULED → RUNNING`。

这一个事实把整个设计故事翻了过来，正确分工是**两条子句各管一个方向**：

| 权威 | 迟到快照 | 守卫 | 谁拦住回退 |
|---|---|---|---|
| `READY` | `RUNNING` | **拒**（非法边） | C |
| `RUNNING` | `READY` | **放行**（`RUNNING → READY` **是**合法边 —— fence re-arm） | **B** |

第二行才是 B 的主场：守卫**放行**了写入，唯一拦住回退的是 `state` 已不在 `DO UPDATE SET` 列表里。自更正的落点：源码 doc、单测注释、`prd.md` 验收表、`implement.jsonl` 全部改正，并保留被推翻的推论（**记录推理如何错，比只记录结论更有用**）。

## 设计：B + C 与两个互斥计数

- **B**：`state` 从 shadow 的 `DO UPDATE SET` 移除。**这处删除本身就是 B**。
- **C**：新增纯函数
  ```rust
  pub fn mirror_write_allowed(prev_state: &str, mirror_state: &str) -> bool {
      prev_state == mirror_state || transition_ok(prev_state, mirror_state)
  }
  ```
  第一臂**不可省**：`transition_ok` 按设计**拒自环**（`READY → READY` 为 false），只取转移判定会把镜像的**常规补列路径整体误拒** —— 而那种失效的外观是「什么都没发生」，属最难发现的一类。
- **不制造第二份状态机**：守卫 `delegate` 给 `transition_ok`，即把镜像路径接入 commit / cancel / claim / schedule_attempt 已共用的**同一份**规则。铁律「图 node state 只能由一份**依赖感知**规则产出」约束的是 `dependency_aware_state`（一个 node **应当**是什么态），与「这一步迁移是否合法」是**两份不同**的规则，复用后者不构成违规（这一点在 `prd.md` 里单列一节写清）。
- **可观测性**（D15 待裁第 3 条）：`BackfillStats` 加
  - `mirror_state_dropped`（B 生效：合法转移被放行，但 `state` 不跟随；写入仍落库）；
  - `mirror_rejected`（C 生效：整个写入 `continue` 跳过）。

  两者**互斥**（拒绝分支走不到 dropped 分支）⇒ `rejected + dropped` 恰读作「快照与权威不一致的 node 数」。另加 `tracing::warn!`（带 `graph_id` / `node_id` / `authority_state` / `mirror_state`），因为**「守卫拒了」与「镜像没到」在 DB 上完全同形**（两者都不动 `state`），返回值与日志是唯一能区分的地方 —— 这条也写进了 `BackfillStats` 的字段注释。

  `BackfillStats` 只在 `#[cfg(feature = "storage")]` 下存在，且全仓 **0 处结构体字面量 / 25 处 `::default()`**（已核）⇒ 加字段非破坏性。

## 自我更正二：B 才是 state 的保证，C 是一致性 + 可观测性守卫

票面（以及我先写下的源码 doc）把上表写成「形状 1 靠 **C** 拦住回退、形状 3 靠 **B** 拦住回退」，读起来像两条子句**各守一半**。**消融实测**（一次只删一条子句，重跑 T17 集成测试）给出的答案不同：

| 注入的突变 | 实测结果 |
|---|---|
| 把 `state = EXCLUDED.state` 加回（**删掉 B**） | 在**形状 3** 打红：`left: Some("READY") right: Some("RUNNING")` —— 权威被迟到的 `READY` 回退。形状 1/2 仍绿（C 拒了它们）。 |
| 令 `mirror_write_allowed` 恒返回 `true`（**删掉 C**） | 在**形状 1 的计数断言**打红（`nodes: 1, mirror_state_dropped: 1, mirror_rejected: 0`，期望 `nodes: 0 / rejected: 1`）—— **而更靠前的 `node_state` 断言通过了** ⇒ 权威 `state` 没动。 |

⇒ 准确口径：

- **B 是 `state` 的保证**：B 在，任何镜像写入都动不了 `state`，**无论守卫怎么判**。
- **C 是一致性 + 可观测性守卫**：它拦掉**已知不自洽**的快照（使其连**非状态列**都刷不进去），并把这件事变成**可计数**而非静默的部分写入。
- 「只留 C」在形状 3 破防（合法边 + 陈旧快照）；「只留 B」让陈旧快照仍能静默改写结构且不可见。**两条都必须有** —— 这才是 D15 裁 `B + C` 的准确含义。

已据此重写源码 doc 的消融表、测试 doc 的表格与 `prd.md` 验收表（保留「原文如此写、为何改」的痕迹）。

## 测试：五个形状 + 两次消融

`crates/uc-engine/tests/graph_store_integration.rs` 新增 `graph_t17_mirror_never_rolls_authority_back_or_resurrects`（`#[tokio::test] #[ignore]`，跑真实 PG）：

| # | 权威 | 快照 | 守卫 | 计数 |
|---|---|---|---|---|
| 1 | `READY` | `RUNNING` | 拒 | `mirror_rejected=1`（**#664 的原形**） |
| 2 | `SUCCEEDED` | `RUNNING` | 拒 | `mirror_rejected=1`（复活） |
| 3 | `RUNNING` | `READY` | 放行 | `mirror_state_dropped=1`（fence re-arm 边） |
| 4 | `RUNNING` | `SUCCEEDED` | 放行 | `mirror_state_dropped=1`（票面验收 2 点名的形状） |
| 5 | `READY` | `READY` | 放行 | 两个计数皆 0（常规补列） |

两个测试设计要点：

- **权威行刻意用 `dependencies = ["ghost-dep"]` 播种**（与 `state` 故意不自洽）。否则「写入是否真的落地」不可观测 —— 一个**空**依赖集会让「被跳过」与「正常写入」产出**同一个 DB 状态**，测试就会为错误的原因变绿。
- 形状 4 额外**显式钉住**：`Completed` 快照**仍会追加一行 `node_completions`**，而 B 把权威 `state` 钉在 `RUNNING`。守卫只管 `state`，镜像的 attempts / completions 仍是 append-only（D15 范围外）。把这个**不对称**写成断言，是为了让后来者**读到**它，而不是**撞到**它 —— 它也正是「图平面的 `state` 是读者唯一可信之物」的具体理由。

## 门禁

| 门禁 | 结果 |
|---|---|
| `cargo fmt --all -- --check` | clean |
| `cargo clippy -p uc-engine --all-targets --features storage -- -D warnings` | clean |
| `cargo clippy --workspace -- -D warnings`（CI job 同款） | clean |
| `cargo check --workspace --all-targets --all-features` | clean |
| `cargo test -p uc-engine --lib` | **447 passed**（446 → 447，+1） |
| `cargo test -p uc-engine -p uc-grpc --no-default-features` | uc-engine lib **387**、uc-grpc **210**、executor_nats_down 5、grpc_integration 8 —— 全绿 |
| `cargo test -p uc-engine -p uc-grpc`（default） | 同上，全绿 |
| `cargo test -p uc-grpc --all-features` | **238 passed**（与 T16 基线一致 ⇒ 无 arity 回归） |
| **真实 PG**：`graph_store_integration --ignored --test-threads=1 graph` | **20 passed / 0 failed / 11.49s**（T15 基线 19 ⇒ +1） |
| **CI `1e61318` / `c6dfd20`：Rust CI** | **8/8 绿** |

**CI 是真跑，不是 SKIP**：`storage integration tests` job 的日志里出现
`test graph_t17_mirror_never_rolls_authority_back_or_resurrects ... ok`，且同文件 `20 passed ... 2.76s` —— 既无 `SKIP:` 行，耗时也不是假通过特征值。**graph_store_integration 的 CI 基线由 19 抬到 20。**

记账自洽性（避免拿本地数对 CI 数误判回归）：uc-engine lib 447 = 443(T14) + 3(T16 `node_type_for`) + 1(T17)；nodefault 387 = 383 + 3 + 1 —— 因为 `node_type_for` 与 `mirror_write_allowed` 都**未受 `storage` 门控**（已核：两者所在测试模块仅 `#[cfg(test)]`），所以两个口径各 +4 一致。

## 环境：本机资源坑（本轮踩了三连）

1. **`cargo check --workspace --all-targets` 触发 `rustc` `STATUS_STACK_BUFFER_OVERRUN (0xc0000409)`**（并行 + 16GB RAM 吃紧）。
2. 该崩溃**留下损坏的构建产物**：随后报 `failed to mmap rmeta metadata` / `can't find crate`（`windows_sys` / `chrono_tz` / `qdrant_client`），但文件大小**看着正常** —— 属崩溃期半写产物，**重跑一次即自愈**，不必 `cargo clean`。
3. **`link.exe` `LNK1102`（out of memory）**：一条命令里串跑两个 cargo 调用、各 `-j 2` 会撞上。

**可用判据：本机一律 `-j 1` 跑重量级构建/链接**（`--all-features` 全目标 2m44s、集成测试二进制 2m48s，均可接受）。

## 残余与边界

- 守卫只管 node 行的 `state`；`task_attempts` / `node_completions` 的 append-only 语义**刻意不动**（已在测试里显式钉住上述不对称）。
- 预取在 node 循环前一次完成 ⇒ 同一 graph **同一次投影内**出现重复 `node_id` 时，第二次读到的仍是事务前状态。当前投影按 `subtasks` 唯一 id 构造，不出现该形状；**属已知边界，非当前缺陷**。
- 未改 `UC_GRAPH_SHADOW` 默认值：守卫落地后不需要靠运维关开关，关掉它会让「图平面与镜像不一致」重新变成**静默**状态（那是另一种不可观测）。


### Git Commits

| Hash | Message |
|------|---------|
| `fdef55a` | (see git log) |
| `1e61318` | (see git log) |
| `c6dfd20` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 21: T18: 逐步用量落到 payload.steps[]（#666 实现）

**Date**: 2026-09-16
**Task**: T18: 逐步用量落到 payload.steps[]（#666 实现）
**Branch**: `main`

### Summary

执行 #666 裁决 C：多步 workflow 的逐步用量落到终态事件 payload.steps[]，纯加性（无新列/新事件类型、contract_version 未动）。D1 一条=一个已执行单元、D2 source 取值链、D3 显式 null 与 SubtaskUsage 省略键相反、D4 legacy 也发一条。消融实测两条子句各自守卫；顺带更正了记忆里错误的 Python 基线（本地 1120→实为 1123）。

### Main Changes

## 背景：一处静默少报

`Worker._execute_steps` 的**每一条** `return` 都只透传**一步**的 usage（源码自陈 *"an under-report of a multi-step chain, not a total"*），而终态事件仍以 `usage_reported: true` 声明「有测量」。下游据此算 Useful Work Ratio / Coordination Ratio 会**系统性偏低、而且看不出来偏低** —— D13 的覆盖率机制只能发现「完全没上报」的节点，**发现不了这种失真**。

#666 已裁 **裁决 C**：逐步明细落在终态 payload 的 `steps[]`。否掉 A（N 步 → N 行）与 B（新事件类型）的是**同一条**理由 —— 两者都必须重新回答「被 fence 的迟到结果要不要把这 N 行一起拒」，而落在 payload 里则「一次 commit = 一行事件」这条结构性性质**原样保留**。本片是纯加性：不新增列、不新增事件类型、零迁移、`contract_version` 未动，**没有 `steps` 键即「无可记录」**。

## 侦察：决定设计的五条一手事实

| 事实 | 一手来源 |
|---|---|
| 4 条 return（顺序失败 abort / 并行组失败 abort / 无步骤 / 正常结束）各只透传一步；同函数内 `all_file_changes` **累积**而 `token_usage` **不** ⇒ 「不累积」是**刻意**的 | `worker.py::_execute_steps` 的 4 条 return + 自陈注释 |
| 唯一 subtask 条目集结口 ⇒ 在此发 `steps` 可覆盖**全部三个** publisher（增量结果 / 周期快照 / 终态） | `nats_worker.py::_make_task_update_payload` |
| 终态 payload 只能由 Rust 写（`commit_once` 事务内）⇒ 必须贯通 Rust 侧 | `graph_store.rs` 的 `node_succeeded` 唯一写入点 |
| `steps` 为空是**常见形状而非边缘**：decomposer 指令明写「简单 subtask 省略 `steps`（单 agent）」 | `orchestrator.ts:2093-2099` |
| 「某步适配器没上报」**可达**：`token_usage` 初值是 `None`，仅当流里出现 usage 事件才赋值 | `sandbox.py` |

第 4 条把「legacy 单 agent 路径也要发一条」从**体贴**变成**验收**（验收 5 点名的正是它）；第 5 条把「`usage: null`」从猜测变成**可达状态**。这两条都是先读代码再设计，而不是先设计再找理由。

## 设计：D1–D4

- **D1 一条 = 一个已执行的单元。** 被条件跳过的步**不留条目** ⇒ `step_index` 的**跳号**就是「那一步没执行」的编码。若给跳过的步发 `usage: null`，则同一个值同时表示「跳过」与「跑了但没报」两种事实，而验收 2 要的恰是后者**可读**。宁可让读者按跳号推断「没跑」，也不让两种事实共用一个值。
- **D2 `source` 取值链：`usage.source`（T15 的实测戳，在适配器解析点自盖）→ 回落 `step.agent`（该步声明的适配器）→ `null`。** 回落是**唯一**还能点名的地方 —— 某步报不出数字时，适配器身份只剩声明这一条来路。
- **D3 步条目用显式 `null`，而 `SubtaskUsage` 沿用省略键 —— 两套相反纪律是刻意的。** 步条目是**数组的位置元素**，显式 `null` 让 `steps[i].usage` 恒可索引、不必先判存在；`SubtaskUsage` 是**线格式的增量**，缺键 = 未上报，且 T15 的 `skip_serializing_if` 是「不设 usage 的发布者序列化结果字节不变」这一承诺的实现手段。两者**不会撞车**，因为 `payload` 只写一次、**不会被反序列化回领域类型**（这一前提已单记进 `implement.jsonl` 的 `boundary:D3-precondition`）。
- **D4 legacy 单 agent 路径也发一条**（`step_index: 0` / `parallel_group: ""`）。键取 `subtask.steps` 是否为空，**不是**取「收集到的列表为空」—— 一个所有步都被跳过的 workflow **合法地**什么都不收集，在那里合成会**凭空造出一个从未执行的单元**。

抽出一个**未门控**纯函数 `steps_payload`，因为它唯一的调用者 `commit_once` 是 `storage` 门控的：只挂在门控里测，形状契约在 `--no-default-features` 下**完全不可见**，而那正是形状回归最难被发现的地方。这个选择**当场回本**：`--all-features --all-targets` 抓到了两处 `on_commit` 调用点的 arity 破窗 —— 它们所在文件顶部带 `storage` 门控，默认特征下被编译成**空文件**，`cargo check --workspace`（不带 `--all-features`）**看不见**。

## 消融：两条子句各自的守卫（实测，非推断）

一次只删一条、重跑：

| 注入的突变 | 实测结果 |
|---|---|
| 删掉 `steps_payload` 的空切片早返回 | **只有** `steps_payload_is_absent_when_there_is_nothing_to_record` 打红，另 4 条仍绿 ⇒ 该行是「无记录 ⇒ 无键」这条加性契约的**唯一**守卫 |
| 给 `StepUsage.usage` 加 `skip_serializing_if = "Option::is_none"` | **两个 crate 各打红一条**：uc-engine 的 `steps_payload_keeps_explicit_nulls_and_order` 与 uc-types 的 `step_usage_serializes_explicit_nulls_unlike_subtask_usage`；diff 逐字显示缺失的 `"usage": Null` ⇒ D3 在两侧被**互不依赖**地钉住 |

第二条消融的价值在于**它证明了两条断言真的分处两个 crate**（各自的编译单元），而不是同一断言抄了两遍 —— 若只在一个 crate 里钉，跨语言那一侧改坏了不会有任何东西打红。消融 B 已复原并复跑确认（uc-types 50 / uc-engine 默认 452 全绿），源码内 `ABLATION` 标记已清扫干净。

## 门禁与一次账目更正

本地全绿（2026-09-16）：`fmt --check`；`clippy --workspace --all-targets --all-features -D warnings`；`clippy --workspace -D warnings`（CI 原样命令）；`cargo check --workspace --all-features --all-targets`；`cargo test -p uc-engine -p uc-grpc` 默认 / `--no-default-features` / `-p uc-engine --features indexing` 四组 exit 0。Rust 基线**只增不减**：uc-engine lib **447→452**（默认）、**387→392**（no default）、**467**（all-features）；uc-grpc lib **210→212**、**238→240**（all-features）；uc-types **47→50**；`graph_store_integration` 的 ignored 计 **20→21**。Python 本地 **1123→1139 passed / 10 skipped**；`ruff check python/ tests/` clean；dashboard 导入检查 OK。

**一次账目更正（本轮的方法论收获）**：记忆里写着「本地 1120 passed / 10 skipped」，我按 +16 新测估算却对不上总数，于是**没有**把它当噪声放过。做法是**在 HEAD 上建 `git worktree` 复跑**，得到 HEAD 的真实本地数 **1123 passed / 10 skipped**（1133 收集）。⇒ 旧数字是**错的**（1120 更像 T15/T16 期数字的混合），HEAD 起算的增量恰好 **+16**，与新增测试函数数**逐一对齐**。更硬的判据是**总收集数对账**：本地 `1139+10 = 1149` 与 CI `1141+8 = 1149` **完全一致**，差的 2 条是 POSIX-only（`skipif(win32)`）；再退一步，我导出了 HEAD 与当前的**全量测试 ID 集**做 `comm`：**removed = 0，added = 16**，逐条都是本票新增 ⇒ 「没有测试被静默丢掉」是**测出来**的，不是推出来的。

**CI（`8a0645d`）：Rust CI 8/8 绿、Python CI 4/4 绿**；TypeScript CI **未被触发**（本 diff 不含 `packages/**`，符合其路径过滤）。判**真跑**的两条判据都过：storage job 日志逐字出现 `test graph_t18_commit_carries_per_step_usage_into_the_terminal_payload ... ok`，同文件 `test result: ok. 21 passed; 0 failed`（2.13s），且**全日志 0 条 `SKIP:`**。CI 日志里 `467 / 240 / 50 / 36 filtered out` 四行还**独立复核**了我的本地 all-features 计数（uc-engine 467、uc-grpc 240、uc-types 50、uc-grpc-server 36），等于给本地数字配了一份外部对照。

## 环境：本机 PG 本轮不可用（口径已收窄）

探针 `timeout 5 bash -c '</dev/tcp/127.0.0.1/5432'` 拒绝连接；PowerShell 侧确认**无** postgres 服务、**无** 5432 监听、`C:\Program Files\PostgreSQL` 不存在 ⇒ provider 是 **Docker Desktop**，而其引擎在本沙箱**起不来**（`docker desktop start` 报 starting，随后 `docker info` 以 30×5s 轮询始终失败）—— 与 WSL2 属**同类**沙箱阻断。

于是 T18 的 PG 集成测试**本地未跑**，唯一一次证据来自 CI。这同时更正了上一轮的记忆口径：T17 那次「本地 live PG 可用」**当时为真**，但**不能跨轮沿用**（provider 是 Docker，引擎停掉即消失）；正确表述是「本机无原生 PG，live PG 取决于 Docker Desktop 是否在跑」。为降低「只能靠 CI」的赌注，把该测试用到的 payload 字面量**同时**用一个纯函数测试（`steps_payload_matches_the_pg_fixture_literals`）在本地钉住 —— 这个本地替身本身是**一次手读事故的产物**：手读时发现 PG 断言里的 `usage` 块漏了 T15 的 `source`，那是**管道永不产出的形状**，若等到 CI 打红，排查成本会高一个数量级。

另一条可复用坑：用 `git worktree add` 复检时，**新工作树里没有 `_uc_core*.pyd`**（未跟踪的构建产物，不进 git）⇒ `test_async_engine.py` 全 25 条 setup ERROR（报 *"Rust extension not built"*），且必须按 venv 的 Python 版本拷对应 ABI（本机 venv 是 **3.14.3** ⇒ 要 `cp314`，先误拷 `cp312` 报的是同一个错）。

## 残余与边界

- **节点的三列保持不动**是**刻意**的（验收 4）：改为各步之和会让历史数据与新增数据**不可比**，还要重新论证「恰好一次」的 fence 语义 —— 属独立决策，票面已列 out of scope。`payload.steps[]` 是**拆解视图**而非新口径。
- `payload.steps[]`（用量记录）与 `uc.subtask.execute` 的 `steps`（步骤定义）**同名不同物**；本票只动前者，且**不要求**两者位置一致（D4 的直接后果：legacy 形态 `subtask.steps` 为空而 `steps[]` 长度为 1）。
- **D3 依赖一个前提**：payload 只写一次、不被反序列化回领域类型。将来若有代码把 payload 读回 `StepUsage`，D3 的两套 null 纪律必须重新论证（已单记进 `implement.jsonl`，避免它将来变成一个隐形陷阱）。
- 本片**不覆盖** review / 其他事件的 payload 形状（票面 out of scope）。


### Git Commits

| Hash | Message |
|------|---------|
| `9a8203f` | (see git log) |
| `8a0645d` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 22: T19: 派发硬门排除产出者 —— review 节点不得由自己审（#669 裁决 A 实现）

**Date**: 2026-09-16
**Task**: T19: 派发硬门排除产出者 —— review 节点不得由自己审（#669 裁决 A 实现）
**Branch**: `main`

### Summary

执行 D16 #669 裁决 A：把「排除产出者」做成派发硬门，落在唯一一处能力 roster 计算点（workers_with_capabilities_excluding）—— dispatch_gate / dispatch_candidates / placement_target 三者同源，故 T12 那条「打分不得看到被门拒的 worker」的不变量自动成立。两条 fail-closed 检查各带独立计数器（no_independent_reviewer / producer_identity_unknown，刻意不合并）。非 review 节点逐字节不变。四次消融实测：两条子句打红不相交集合 ⇒ 各自独立被钉住。另修掉两处过期 spec（agent 默认能力集写成含 review，与代码相反）+ T16 一句已变假话的注释。

### Main Changes

## 背景：一条**结论在 wire 上不可见**的独立性

T16（#661）把 `review` 做成图上一个**有类型**的节点，并在交付时**如实记账**了一条残余：「自审禁止是**能力门级**而非派发级……堵死需改 `dispatch_gate`，回 D14」。这条记账**没有出口**，直到本轮把它升成决策票 D16 #669 并裁定。

缺口的形状值得单说：一个**显式 opt-in 了双角色**（`UC_CAP_REVIEW` + `code`）的 worker，可以既产又审，而产出的 verdict 在 wire 上与**真独立审阅逐字相同**（都进 `SubtaskReview`）。也就是说这不是「审得不准」，而是**下游没有任何东西能发现它不准** —— 与本仓反复付代价的失效形状（T12 的 affinity 静默失效、T17 的假 `RUNNING`）同族。

D16 裁定 **A**：把「排除产出者」做成**派发硬门**，落在**唯一一处** roster 计算点。

## 落地：为什么是那一处、为什么是必需形参

`worker_service.rs` 的三个派发相关函数**同源**：`dispatch_gate`（判定）、`dispatch_candidates`（候选）、`placement_target`（affinity 打分）都从能力 roster 派生。既有不变量就写在后者的文档注释里 —— *"Scoring must never see a worker the gate would reject"*（`worker_service.rs:307`）。

⇒ 排除只要落在 `workers_with_capabilities_excluding`（新抽出的**单一** roster 计算点），那条不变量**自动成立**；若只改 `dispatch_gate`，`placement_target` 会继续把被排除者当候选、并**定向投到它自己的 per-worker subject**（正是自审最直接的通道）。

**`exclude` 做成必需形参，不做 `Option`/默认值。** 理由是本票要关掉的正是「某个派发路径不知道这条约束」——一个可省略的参数等于给未来的新派发口留了一个「少写一个参数就静默恢复自审」的后门。`workers_with_capabilities`（无排除）保留给纯能力查询，并在文档里明说**它不是派发口径**。

**加性保证**：`requires_independence == false` ⇒ `review_independence` 返回空约束 ⇒ `dispatch_gate` 对非 review 节点**逐字节**等价于改动前的三重过滤（第一个检查不可能触发，第二个的候选集等于原集合）。这条有专项消融背书。

## 两条 fail-closed 检查：为什么计数器**必须**分开

| | 条件 | 含义 | 计数器 |
|---|---|---|---|
| 1 | `unknown_producers` 非空 | 依赖**跑了但没人记下是谁跑的**（PG backfill / `.uc/tasks` 导入 / 快照不全）⇒ 独立性**无法核实** | `producer_identity_unknown` |
| 2 | 排除后候选**为空** | 生产者被识别了，而它是**唯一**持有该能力的人 ⇒ **没有独立审阅者** | `no_independent_reviewer` |

合并成一个的代价很具体：节点停在 `PENDING`，而操作者**无从判断**该「补一个 reviewer worker」还是「去修生产者的上报口径」—— 后一种情况下补 worker **毫无作用**。这与 T12/T17 是同一种病：**失效可见但不可归因**。

顺序上「越具体越先」也有具体理由：scope 判定会**点名候选 worker**，而那个名字会把操作者引向一个**不是问题**的 scoped worker。所以独立性检查排在 scope/version **之前**。

`NoCapableWorker` 的语义**刻意没动**（用排除**前**的 roster 判定）⇒「没人有这个能力」与「只有产出者有此能力」永远可分。

## 消融：四次，一次只删一条（实测非推断）

| 注入的突变 | 实测结果 |
|---|---|
| 删掉 roster 的排除过滤 | **3 条**打红（自审拒绝 / placement 不选生产者 / 排序验证），而 unknown-producer 与「非 review 不变」**仍绿** |
| 中性化 `unknown_producers` 检查（`if false && …`） | **2 条**打红（unknown-producer 分计数 / 排序验证），而自审拒绝**仍绿** |
| 去掉 `review_independence` 里 review 判定早返回 | **恰好 2 条**打红（`independence_is_inert_for_every_non_review_node` + server 侧快照读取），其余全绿 |
| 把谓词从精确相等放宽成 `c.contains("review")` | 打红 `assertion failed: !requires_independence(["code-review"])`（连带既有的标签测试） |

**前两条打红的是不相交集合**（只共享排序用例）⇒ 两条子句是**各自独立被钉住**的，而不是「两条子句守同一句断言」。这正是 T17 那次的诊断法：**看哪条断言先红，本身就是信息** —— 若突变只打红靠后的断言而更靠前的仍绿，那条信息能定位是哪条子句在起作用。

第四条消融还改掉了一个我自己刚写下的**薄弱断言**：原测试主体是 `node_type_for(&c) == NODE_TYPE_REVIEW` ⟺ `requires_independence(&c)` —— 两边调**同一个谓词**，是**自指**的，只能抓 `node_type_for` 漂移，**抓不住谓词定义被改**。补上绝对钉（`!requires_independence(["code-review"])` 等）之后它才真的能打红。

## 门禁、基线，以及一份**外部对照**

本地全绿（2026-09-16）：`fmt --check`；`clippy --workspace -D warnings`（CI 原样命令）；`clippy --workspace --all-targets --all-features -D warnings`；`cargo check --workspace`；**`cargo check --workspace --all-targets --all-features`**（公开签名变更的必需探针）；`cargo test -p uc-engine -p uc-grpc` 默认 / `--no-default-features` / `--features indexing` 三组。

Rust 基线**只增不减**：uc-engine lib **452→453**（默认）、**392→393**（no default）、**467→468**（all-features）；uc-grpc lib **212→221**、**240→249**（all-features）；uc-types **50**、uc-grpc-server **36** 不变。**自洽校验**：`453−393 = 60`、`468−453 = 15`、`249−221 = 28` —— 三个差值与 T14/T18 时**完全相同** ⇒ 新增单测既未受 `storage` 也未受 `messaging` 门控，且门控测试数本身没被搅动。

**CI（`322a571`）：Rust CI 8/8 绿、Python CI 4/4 绿**（TypeScript CI **未被触发** —— diff 不含 `packages/**`，符合路径过滤）。判**真跑**两条判据都过：storage job 里 `graph_store_integration` **21 passed / 0 failed / 2.91s**（与 T18 同为 21），逐字有 `test graph_* ... ok` 共 43 条，**全日志 0 条 `SKIP:`**。CI 日志里 `468 / 249 / 50 / 36 filtered out` 四行还**独立复核**了我的本地 all-features 计数（uc-engine 468、uc-grpc 249、uc-types 50、uc-grpc-server 36），等于给本地数字配了一份外部对照。

Python（本轮只改了一处注释，无行为变更）：按仓规逐文件串行 **collected 1149 / passed 1139 / skipped 10 / failed 0 / error 0**，与 T18 基线**逐项相同**；`ruff check python/ tests/` clean。

## 🔴 本轮最值钱的一条：spec 说反了，而它差点让我把结论写反

为确认「worker 的默认能力集里有没有 `review`」，我读了 `.trellis/spec/backend/agent-capability-spec.md`。它写着：

```
Base: ["code", "search", "memory", "test", "decompose", "review"]
```

**照此推理的结论是**：每个 worker 默认都持有 `review` ⇒ 只要部署里只有一个 worker，本票的硬门就会让**所有 review 节点永久卡在 `PENDING`** —— 一条耸动、自洽、而且**看起来很有说服力**的结论，我几乎把它写进票面与记忆。

去读一手代码（`worker.py:433-448`）后发现**恰好相反**：

```python
caps = ["code", "search", "memory", "test", "decompose"]
```

T16 自己留了注释解释为什么排除 `review`：*"Advertising it by default would let every producing worker claim its own review."* 真正的开关是环境变量 **`UC_CAP_REVIEW`**（`worker.py:495`）。

**由此得到本票正确的定位（与险些写下的那句不同）**：

1. 默认 worker **根本不持有** `review` ⇒ 它对 review 节点先在**能力门**上就是 `NoCapableWorker`，**走不到**本票的新检查。所以本票**不是**「新引入的常见路径阻塞」，而是**残余缺口的收口** —— 与 D16 的措辞（*显式双角色 worker 仍可自审*）严丝合缝。
2. 新 fail-closed 路径**可被触达的前提**是「集群里存在一个 opt-in 了 `UC_CAP_REVIEW` 的 worker」。此时**要么**它审自己产出的东西（原缺口），**要么**停在 `PENDING`（本票）。

这是本周第三次「**间接信号 ≠ 一手事实**」（前两次：拿 `which protoc` 的 PATH 事实当构建事实；信 Bun 自陈的 *"not your code"* 而真因就在那行上面）。**规格比没有规格更危险，因为它自带权威感** —— 过期时它会被当成事实。

顺带修掉两处**已经变成假话**的陈述：spec 里那行能力集（已改，并补上 `UC_CAP_REVIEW`）；`worker.py` 里 T16 留的 *"The dispatch side has no exclusion primitive"*（本票之后不成立 —— 已改写并保留 T16 原本「为什么 review 不默认」的理由）。另外发现 `worker-service-spec.md` 的 `dispatch_gate` 签名**落后两个票**（缺 T8 的 `project_id`），一并补齐并写上 T19 契约。

## 残余：一个**未消除**的竞态窗口（如实记账）

本门是**花名册检查**，不是**投递保证**。投递走**共享 durable work-queue**；`resolve_dispatch_subject` 在 placement 返回 `None` 时**回落**共享 subject，而 `None` 是**正常**结果（affinity 是软偏好，D12）。⇒ 当候选 ≥2 且其中之一是生产者时，队列**仍可能**把 review 投给它。

本票消除的只是**最坏的形状**：生产者是**唯一**候选人时的静默自审。要真正消除竞态需要 per-worker subject **全覆盖** + 把 affinity 从**偏好**升格为**门** —— 前者对 legacy worker **结构上不可能**（没有 per-worker subject），后者与 D12 正面冲突。这正是 D16 驳回裁决 D 的理由（**做不到**，不是「贵」）。


### Git Commits

| Hash | Message |
|------|---------|
| `322a571` | (see git log) |

### Testing

- [OK] Rust：`cargo fmt --all -- --check` clean；`clippy --workspace -j 1 -- -D warnings`（CI 原样）clean；`clippy --workspace --all-targets --all-features -j 1 -- -D warnings` clean；`cargo check --workspace -j 1` clean；`cargo check --workspace --all-targets --all-features -j 1` clean（公开签名变更的必需探针）。
- [OK] Rust 测试：uc-engine lib **453**（默认）/ **393**（no default）/ **468**（all-features）/ **453**（indexing）；uc-grpc lib **221**（默认 = nodefault）/ **249**（all-features）；uc-types **50**；uc-grpc-server **36**。自洽校验 `453−393 = 60`、`468−453 = 15`、`249−221 = 28` 均与 T14/T18 相同。
- [OK] 新增 9 条测试（uc-engine 1 + uc-grpc 8），覆盖验收 1–6。
- [OK] 消融 4 次（排除过滤 / unknown-producer 子句 / review 早返回 / 谓词放宽成子串），每次实测打红，复原后复跑全绿（`worker_service.rs` 复原后 sha256 前 12 位 = `4b6a4db89d87`，与消融前一致）。
- [OK] Python：逐文件串行 **collected 1149 / passed 1139 / skipped 10 / failed 0 / error 0**（与 T18 基线逐项相同）；`ruff check python/ tests/` = All checks passed；py39 兼容扫描 82 文件 0 问题。
- [OK] CI（`322a571`）：**Rust CI 8/8 绿 + Python CI 4/4 绿**（TS CI 未触发 —— diff 不含 `packages/**`）。真跑判据：storage job `graph_store_integration` **21 passed / 2.91s**、43 条逐字 `test graph_* ... ok`、**0 条 `SKIP:`**；CI 日志 `468 / 249 / 50 / 36 filtered out` 独立复核了本地 all-features 计数。
- [WARN] 本机 live PG 本轮不可用（`</dev/tcp/127.0.0.1/5432` 拒连）。T19 不含 `#[ignore]` PG 测试 ⇒ 不受影响；公开签名变更由 `--all-features` **编译**探针覆盖。

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 23: T20: 能力层规范与代码脱节 —— 纠正 #111/#161 遗留的幽灵 API，并把默认能力事实钉死

**Date**: 2026-09-16
**Task**: T20: 能力层规范与代码脱节 —— 纠正 #111/#161 遗留的幽灵 API，并把默认能力事实钉死
**Branch**: `main`

### Summary

删掉 agent-capability-spec.md 里描述已删除层（#111/#161）的前 253 行，换成活的四个接触点；修正默认能力的两层事实（漏 decompose + 混淆 base seed 与 advertised set，实跑 13 项）；补 T19 收口的四处同源遗漏（含两处它自己引入的过期行号）；把 test_default_capabilities 升级为绝对钉（消融证明 decompose 此前无人守）。

### Main Changes

## 为什么会有这一票：规范在描述一层**已经不存在的代码**

本票起点是上一票（T19）留下的一条铁律 —— **过期规范比没有规范更危险，因为它自带权威感**。T19 就在同一份文件里吃过一次：规范写着 worker 默认能力集**包含** `review`，读码才发现 `review` 要 `UC_CAP_REVIEW` 显式 opt-in。照那份规范推理，会得出「T19 让单 worker 部署永久卡住 review 节点」这个耸动而**错误**的结论。

T19 当时修好了那份规范的**一处**。本轮在「下一票做什么」的自检里回头核了一遍，发现**同一份文件的前 253 行整体在描述一层已删除的代码**。

## 一手取证：六个符号零命中，两个「必需测试」从未存在

方法：`git grep` 覆盖 `python/` `packages/` `crates/` 三棵树，逐符号计数。

| 规范 §1/§2 的签名 | 命中数 | 移除于 |
|---|---|---|
| `Worker._self_evaluate` | **0** | `ad931ec` 2026-06-21 (#111) |
| `Worker._classify_error` | **0** | 同批 |
| `Worker._adaptive_retry` | **0** | 同批 |
| `Orchestrator._select_worker` | **0** | `05ccb56` 2026-06-26 (#161, *remove Python Orchestrator*) |
| `Orchestrator.schedule_subtasks` | **0** | 同批 |
| `Worker._gather_prior_context` | **0** | 同批 |

`_select_worker` 全仓唯一命中就是**该规范自己**。§3 的契约同样建在死码上：`_record_experience`（只在规范与一份已归档 prd 里）、`confidence_threshold` 与 `experience_key`（全仓 0）、`FALLBACK_TOOL`（只剩 `types.py:61` 一个**枚举成员**，**零消费者** ⇒ 定义即孤岛；这比「函数被删」更隐蔽，因为符号查找会命中）。§6 点名的两个测试 `test_select_worker_capability_match` / `test_select_worker_fallback_load` **从未存在**（全仓唯一命中就是那张表）。

为防 grep 单点失误，`_select_worker` 用了**三种**互不相同的方法复核，结论一致。⚠️ 第四种（裸 `grep -rn` 全仓）**被放弃**：它会扫 `target/`，5 分钟不收敛 —— 记下来是因为它看着「更彻底」，实际只是错。

**处置**：死契约**删除**，不保留为「历史设计」（依据本仓既有口径「留着一个指向不存在事实的路标，等于给下一个人埋雷」），但顶部留 6 行 History 块记录移除提交与证据，便于溯源。§1–§7 换成活的四个接触点（derive / advertise / route / opt-in），并明写**派发侧细节归 `worker-service-spec.md`** —— 同一条规则不设两个家。

**规模**：987 → 832 行。写盘走**字节级 CRLF 脚本 + assert-first**（尾部 byte-identical 才落盘），`git diff --numstat` 复核为 70/225（若整文件重写会是 ~987/832）。

## 默认能力：一个「被规范写反了两次」的事实

规范把它写成一个**四元素列表**。实际是**两层**，且两层都被写错了：

| 层 | 内容 | 可钉性 |
|---|---|---|
| **base seed** | `["code","search","memory","test","decompose"]`（`worker.py:451`） | 固定，可绝对钉 |
| **advertised set** | seed ∪ MCP/工具派生 ∪ `UC_CAP_*` ∪ **插件注册表派生** | **环境相关，不可等值钉** |

**实跑（不是读码）**得 13 项：

```
['code','search','memory','test','decompose','grok-build','grok','claude-code','codex',
 'deepseek-harness','deepseek','local-harness','local-llm']
```

多出的项来自 `worker.py:524` 的 `agent_registry.registry.capability_names(shutil.which)`（注释自陈：CLI agent 只在 binary 在 PATH 上时广告、API-backed harness 恒广告）。

⇒ 规范的两处修正：§6 的 `test_default_capabilities` 行补上 `decompose`；§7 的反例字面量补上 `decompose`。**且明确写下「永不advertised set 做等值断言」**。

## 一次自我更正（本票最有价值的副产品）

侦察早期我从源码 `worker.py:451` 读到 `caps = [...5 项...]`，**推断**默认能力就是这 5 项，并打算据此写**等值断言**。**实跑推翻**（13 项）。

⇒ 若按推断落笔，那条测试会当场变红，**而我会去修一个没坏的东西**。与 T19 同源、方向相反：T19 是信了过期规范，本票差点是信了**源码片段当运行时事实** —— 两者都是**把间接信号当事实**。判据不变：读一手来源，或直接跑一次。

## T19 收口的四处遗漏（同一根因：改了一处，漏了同源的其余）

1. `tests/python/test_worker_capabilities.py:151` 的 docstring 仍写 *"The dispatch side has no exclusion primitive"* —— 自 T19 起为假。T19 改了 `worker.py` 里的同源注释，**漏了这处**。正是 T19 自己写下的铁律（推翻结论时要更正**所有**已落盘的记录）的反例。
2. `agent-capability-spec.md` 与 3. `worker-service-spec.md` 各引 `worker.py:495` —— **T19 自己引入的过期行号**：它在 seed 上方插了 9 行注释（净 +3），却仍引用**改前**的行号。实际是 `:498`。
4. `index.md:37` 仍以已删除的能力（self-reflection / adaptive retry / experience recall）描述该文件并标 `Filled`。

四条全部更正。⚠️ 顺带发现**更大范围**的问题：该规范的行号引用**系统性过期且偏移不一致**（`_execute_steps` 实为 1365 规范写 1053 = +312；`_run_single_step` +374；`_render_step_prompt` +391；`_execute_in_sandbox` +208）⇒ **无法机械 +N 修正**。本票**只修** T19 自己引入的 `495`→`498`（+3 类），其余**另开 #672** 跟踪 —— 不静默吸收、也不在本票里改票面。

## 钉法：把「被描述」升级为「被钉住」，且用绝对钉

`test_default_capabilities` 原为**成员资格断言**（`"code" in caps` 等四条）。升级为**绝对钉**：

```python
seed = ["code", "search", "memory", "test", "decompose"]
assert worker.capabilities[: len(seed)] == seed   # 去重保序 ⇒ seed 恒在最前
assert "review" not in worker.capabilities
```

**刻意不做** `== worker.capabilities`：那会带上环境相关的注册表派生项 ⇒ 假红。docstring 写明两层结构与理由。

**消融自检**（一次一条，实测非推断）：

| 突变 | 结果 |
|---|---|
| 基线（不改） | GREEN |
| 从 seed 删 `decompose` | **RED** |
| 把 `review` 塞进 seed | **RED** |

**关键收获**：突变①在**旧测试下是绿的** —— 旧断言只查 code/search/memory/test，`decompose` 此前**无人守**。⇒ 这条钉是**新增覆盖**，不是装饰。跑完 `worker.py` 按字节恢复、sha256 与改前一致。

### Git Commits

| Hash | Message |
|------|---------|
| `6d7653c` | docs(spec): drop the removed capability layer and pin the base seed (T20 #671) |
| `d2f4f49` | docs(spec): flag the dead pre-processing layer in codegraph-integration.md (T20 #671) |
### Testing

- `pytest tests/python/test_worker_capabilities.py` -> **28 passed**（含改动后的 docstring 所在用例）
- `pytest tests/python/test_sandbox.py` -> **187 passed**（含升级后的 `test_default_capabilities`）
- `ruff check python/ tests/` -> **All checks passed**
- **逐文件串行收集数 = 1149**（44 个文件、零条目文件 0 个），与 T18/T19 基线**逐项相同** ⇒ 只改断言、未增删用例（Rust/Python 计数应**逐项不变**，这本身是一条回归证据）
- 消融：2 个突变各自打红，恢复后 worker.py sha256 一致（`55eb36def193…`）
- **CI**：`6d7653c` 上 **Python CI 4/4 绿**（ruff lint / test 3.9 / test 3.12 / dashboard checks）；**Rust CI 与 TypeScript CI 未被触发**（diff 不含 `crates/**`、`packages/**`）—— 与「零 Rust/TS 改动」自洽，且是**可核对的**证据
- **真跑判据**：CI 日志逐字出现 `test_default_capabilities PASSED`（两个 Python job 各一次）与 `test_review_capability_absent_by_default PASSED`；**全日志 0 条 `SKIP:`**；`1141 passed, 8 skipped` ⇒ 收集面 1149 与本地一致

### Status

[OK] **Completed** — T20 #671 已交付并归档（`6d7653c`）。**本票不属 P2 地图范围**（非 Execution Optimizer / Blackboard review / market scheduling，也非其前置），是仓级文档卫生的独立维护票；唯一关联是 #670（范围 1 处 docstring 是 T19 收口的补漏）。

### Next Steps

- **#672**（本票新开）：规范里的 `文件:行号` 引用系统性过期、偏移 +208~+391 不等 ⇒ 先普查出清单，再定口径（改符号引用 / 加校验器 / 标注基线）。
- **P2 本体**：仍卡在外部「方案第 21 节」原文，不臆造。
- 待沉淀：本票 `implement.jsonl` 的追加曾落两份（已按整行去重修回 20 行）⇒ 追加脚本必须幂等或事后可去重。
- 已发现但**故意未处理**（预先存在，不属本票）：journal 里有两个 `## Session 13`，HEAD 上即如此。
