# Orchestrator并行推进：omp Phase3核心 + Python全链路优化

## Goal

并行推进两个 Orchestrator 系统：omp TypeScript 侧重构的 Phase 3-4 收尾，Python 侧重全链路优化的关键缺口修复。优先修复影响功能正确性的 bug（Python 自动调度），再补齐高 ROI 的增强项。

## What I already know

### omp 侧 (packages/uc-orchestrator/)
- Phase 1-2 完成：extension.ts, orchestrator.ts, scheduler.ts, grpc-bridge.ts, task-store.ts, agents/*.md
- Phase 3 大部分完成：memory-bridge.ts 已提取，gRPC bridge 完整
- 缺口：SubtaskResult 的 modifiedFiles/recentToolCalls 声明但未填充，toPersisted() 丢弃这些字段
- Phase 4 未动：IRC、advisor、动态容量、子任务级 checkpoint/resume

### Python 侧 (python/ultimate_coders/agent/)
- **关键 bug**：handle_subtask_result 不调用 schedule_ready_subtasks，DAG 执行在第一波后卡住
- 事件去重：仅 NATS 消息级去重，无语义去重
- SubtaskResult：NATS 更新只传 summary 字符串，丢失 modified_files/stderr/tool_calls
- Checkpoint：无自动触发、无版本化、recover_task 不恢复活对象
- Heartbeat：WorkerInfo.last_heartbeat 从不刷新，超时阈值不一致(60s vs 90s)
- SSE fallback：只发快照不发单事件，ring buffer 未被 SSE 读取

## Requirements

### R1: Python 自动调度修复 (Critical Bug)
- handle_subtask_result 完成子任务后必须调用 schedule_ready_subtasks
- 新就绪的子任务应自动分配给可用 Worker
- 发出 subtask_ready 事件

### R2: Python 事件去重
- 应用级去重 key = (task_id, subtask_id, event_type, dedup_window_5s)
- TaskEventEmitter ring buffer 添加去重
- NATS message_id 改用语义 key 而非纯时间戳

### R3: Python SubtaskResult 详情回流
- SubtaskResult 添加结构化字段：modified_files 含 diff 统计、retry_count、error
- handle_subtask_result 持久化完整 result 到 engine memory
- NATS 更新 payload 包含 modified_files 和 error

### R4: Python Checkpoint 增强
- 子任务完成后自动 checkpoint
- checkpoint 添加版本号
- recover_task 恢复活 Task 对象到 self.tasks

### R5: Python Heartbeat 修复
- Worker.send_heartbeat 更新 Orchestrator.workers[id].last_heartbeat
- 统一超时阈值（60s → 90s 或配置化）
- Worker 核心类添加心跳循环

### R6: omp Checkpoint 字段填充
- executeSubtask 填充 SubtaskResult.modifiedFiles 和 recentToolCalls
- toPersisted() 保留这些字段
- TaskStore JSON 包含完整结果

### R7: omp 子任务级 Resume
- resume 支持跳过已完成子任务，只重跑失败/未完成的
- 而非重跑整个 wave

## Acceptance Criteria

- [ ] handle_subtask_result 后自动调度下一波子任务（Python）
- [ ] 5s 窗口内相同 (task_id, subtask_id, type) 事件只保留一条（Python）
- [ ] SubtaskResult 包含 modified_files/retry_count/error 并持久化（Python）
- [ ] 子任务完成后自动 checkpoint（Python）
- [ ] Worker.send_heartbeat 刷新 Orchestrator 侧 WorkerInfo（Python）
- [ ] omp SubtaskResult.modifiedFiles/recentToolCalls 被填充和持久化
- [ ] omp resume 跳过已完成子任务

## Definition of Done

- 关键路径测试通过
- cargo check / maturin develop 无报错
- Lint / typecheck green

## Out of Scope

- Event Sourcing via NATS（已有基础，非 MVP）
- Dashboard SSE fallback 增强（低 ROI）
- gRPC WatchDashboard 增量推送（Rust 侧已有，Python 侧非 MVP）
- omp Phase 4（IRC、advisor、动态容量）
- tools/ 目录拆分（当前 2 文件结构够用）

## Technical Notes

- Python orchestrator.py:2278 行，handle_subtask_result 在 L506-679
- Python event_emitter.py:117 行，ring buffer 无去重
- omp orchestrator.ts:947 行，SubtaskResult 在 L50-54，toPersisted() 在 L787-808
- omp task-store.ts:93 行
