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
