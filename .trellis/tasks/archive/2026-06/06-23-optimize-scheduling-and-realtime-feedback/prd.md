# Optimize Scheduling and Realtime Feedback Pipeline

## Goal

优化 UltimateCoders 任务调度与实时反馈全链路：8 个优化点全部实现。

## Requirements

1. **local_worker 并行调度**：对齐 nats_worker 的 asyncio.gather 模式，ready subtasks 并行执行
2. **SSE 延迟优化**：0.5s 轮询改为 NATS callback 直接推入 generator
3. **TUI 事件去重**：gRPC 双通道 content-key 去重
4. **拆分质量验证 + 重试**：decompose 后验证合理性，不合理则 re-decompose（1 次）
5. **Dashboard 增量快照**：增量事件驱动 + 按需全量同步
6. **Worker 弹性并发**：按子任务类型动态调整 max_capacity
7. **子任务 checkpoint**：sandbox 中间结果持久化，失败恢复跳过已完成步骤
8. **冲突检测集成**：sandbox 执行前后自动 declare/release EditIntent

## Acceptance Criteria

* [x] local_worker._execute_subtasks 并行执行多个 ready subtasks
      — 不再适用：`python/ultimate_coders/local_worker.py` 已于 #171
      （a368371，2026-06-27）随手写式 JSON-RPC 桥一并删除，改为 connectrpc
      gRPC-Web。目标模块不存在，此项按「失效」结案，而非「未做」。
* [x] SSE 事件推送替换轮询；本机 NATS → HTTP SSE 实测 < 200ms
      — `dashboard/app.py` `_sse_subscribers: set[asyncio.Queue]` +
      `_subscribe_sse()`，NATS 回调直推队列，不再 0.5s 轮询。2026-10-09
      实测 3 个客户端各 300 事件，持续流最大 6.394ms、突发流最大 55.712ms，
      零丢失/重复。边界为本机 NATS publish 到完整 HTTP SSE 帧，不含浏览器
      渲染或跨主机传输；不是 CI 性能 SLA。证据和复现命令见
      [live verification](../../../../../docs/live-feedback-and-metainfer-verification.md)。
* [x] decompose 结果验证 + 一次 re-decompose
      — `orchestrator.validate_decomposition()` + `_decompose_task` 内恰好
      一次重拆，携带失败原因。原先静默丢弃缺描述项与越界依赖、且完全不查
      依赖环；现在这些都判为「不合理」并触发重拆，二次仍不合理则回退
      newline-split。回归测试 `tests/python/test_orchestrator_decompose.py`
      （含环检测、自依赖、退化同名）。
* [x] TUI 端无重复事件显示
      — TUI 面已被 Dashboard 取代，等价能力落在
      `dashboard/src/hooks/useDashboardGrpc.ts`：content-key
      （type:task_id:subtask_id:timestamp）+ 有界 seen-set（500 条插入序淘汰）
      + F70 长期重放去重。较原定 1s 时间窗更强：身份键不会过期后重新进入。
* [x] Dashboard 增量事件驱动（非纯轮询）
      — `dashboard/app.py` 周期全量快照为 incremental-first，事件流已驱动。
* [x] Worker 按子任务类型动态调并发
      — `python/ultimate_coders/agent/worker.py` `_dynamic_capacity(subtask)`，
      由 `nats_worker.py` 消费。
* [x] 子任务 checkpoint 持久化 + 恢复
      — `crates/uc-engine/src/checkpoint.rs` `CheckpointManager` +
      `worker.py` `_save_checkpoint` / `_load_checkpoint`（按 attempt 隔离）。
* [x] sandbox 执行自动 declare/release EditIntent
      — 收口到 `nats_worker._execute_subtask_with_context`：两条执行路径
      （本地批量、JetStream）统一在此声明/释放，且用 `try/finally` 保证异常
      也释放。原先 straight-line 释放会让异常泄漏 intent，JetStream 路径
      则完全没声明。另修 `ConflictDetector.remove_intent` 遗留空键的无界累积。
      回归测试 `tests/python/test_nats_worker_helpers.py::TestEditIntentLifecycle`。

## Definition of Done

* 单元测试覆盖新逻辑
* Lint / typecheck / CI green
* 不破坏现有 API 契约

## Out of Scope

* 新外部依赖
* proto/API breaking change

## Technical Approach

1. local_worker: 复制 nats_worker._execute_subtasks 的 gather 模式
2. SSE: NATS subscription callback → asyncio.Queue → generator 直接 yield
3. TUI: useTaskEvents 加 content-key + 1s 窗口去重
4. decompose: 加 _validate_decomposition() + 1 次 retry
5. Dashboard: 改 snapshot 为增量 delta，按需 full sync
6. Worker: 加 _dynamic_capacity(subtask) 方法
7. Checkpoint: SubtaskResult 持久化到 engine.write_memory，恢复时读取
8. Conflict: _execute_in_sandbox 前后自动 declare/release

## Technical Notes

* 关键文件：nats_worker.py, local_worker.py, orchestrator.py, worker.py, dashboard/app.py, useDashboardGrpc.ts, useGrpcWeb.ts, ChatLog.tsx, useTaskEvents.ts
