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

* [ ] local_worker._execute_subtasks 并行执行多个 ready subtasks
* [ ] SSE 端到端延迟 < 200ms
* [ ] decompose 结果验证 + 一次 re-decompose
* [ ] TUI 端无重复事件显示
* [ ] Dashboard 增量事件驱动（非纯轮询）
* [ ] Worker 按子任务类型动态调并发
* [ ] 子任务 checkpoint 持久化 + 恢复
* [ ] sandbox 执行自动 declare/release EditIntent

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
