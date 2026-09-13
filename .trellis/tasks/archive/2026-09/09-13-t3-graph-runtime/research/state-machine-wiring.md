# Research: 状态机接线面（T3 前置勘察，HEAD 8f82b7bb，2026-09-13）

## 1. T2 留下的 GraphStore 面
- 纯函数：`normalize_status_token`(:76)、`node_state_token`(:96，已含 READY/RUNNING/SUCCEEDED/FAILED/CANCELLED/PAUSED 字符串 token)、`attempt_id`(:133)、`project_task`(:224)、`run_migrations`(:416，advisory-lock 样板)。
- `GraphStore`(:591)：`connect/with_pool/is_connected`、私有 `write_projection`(:650-763，单事务：graph/node DO UPDATE(shadow)|DO NOTHING(import)，attempts/completions **永远 append-only DO NOTHING**)、`backfill_from_tasks_table`(:770)、`import_tasks_dir`(:839)、`upsert_task_shadow`(:920)、`shadow_diff`(:929)。
- **零转移逻辑**：version 硬编码 1（:664/:671），无 CAS，`node_completions` 只有 PK 约束（:519-537 注释"logic is T3"）。
- `GraphShadowSink` trait(:394) 仅 `shadow_persist`，always-compiled（uc-grpc 无 sqlx，注入 `Option<Arc<dyn>>` 模式，setter server.rs:696，触发点 persist_task:682-688）。

## 2. 网关现状（要挂钩的准确位置，T2 后行号）
- `update_subtask_status` def :1558；非测试调用 10 处：publish_ready_subtasks :2189/:2218/:2265/:2271/:2279、dispatch_ready_subtasks :3030/:3057/:3110/:3116/:3124。
- 双派发口：`publish_ready_subtasks`(:2165) 只被 gRPC update_task(:4311) 调；`dispatch_ready_subtasks`(:2998 free fn) 被 NATS update 订阅(:2626) 与 reaper(:2945/:2985) 调。**T3 两个口都要挂 schedule 动词**。
- Reaper `spawn_heartbeat_monitor`(:2899-2991)：30s tick；`mark_stale_workers`(:1456)→`reassign_stale_subtasks`(:1473，InProgress/Assigned→Pending，无计数)；`reassign_stale_assigned_subtasks`(:1596，300s，Assigned→Pending+dispatch_retry_count+1 :1621)；`mark_stale_tasks_failed`(:1684)。**测这些行为的都在 server.rs cfg(test) :5260-5600**（reassign 系 3 个 + dereg 回归），T3 不动 legacy 则不破。
- 结果入口：`NatsTaskUpdate`(:94，无顶层 worker_id；归因只靠 `NatsSubtaskUpdate.assigned_worker` :143)→`apply_update_with_metadata`(:1267-1333 改状态/结果，:1309 worker_id 从 assigned_worker 派生)→persist :1384。message_id 去重 :2529。
- 类型事实：`SubtaskStatus`(:168 Pending/Assigned/InProgress/Completed/Failed/Conflicted)，**字符串线上无 int 映射**；`Subtask.dispatch_retry_count`(:97 网关加) vs `retry_count`(:120 worker 报)。
- 9 态 NodeStatus 枚举不存在，任何 crate 都没有。

## 3. 关键设计张力（PRD 裁决）
票面写"替换 reaper 的 reassign 逻辑"，但 HashMap 仍是行为权威到 T6——两套 reaper 并跑必然漂移。裁决：T3 的"替换"=**图平面长出正确的 attempt 生命周期转移**（超时→fence→READY 在 node/attempt 行上真实发生并被测试），legacy reaper 原样保留；T6 切主时删 legacy。commit-once 在图平面即刻生效：**双写竞态下晚到的 legacy 结果无法让图平面出现双 winner**——正确性从本票起就有实体，不靠 T6 承诺。
