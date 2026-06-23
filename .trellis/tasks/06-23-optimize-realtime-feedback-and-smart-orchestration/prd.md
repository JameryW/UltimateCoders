# Optimize Realtime Feedback and Smart Orchestration

## Goal

6项优化：Worker实时流式反馈、gRPC分解对齐Python、子任务DAG可视化、智能重拆分、Worker池弹性扩缩、Checkpoint持久化到TiKV。

## Requirements

1. **Worker 输出实时流式反馈**: sandbox 执行 stdout 行级解析 → tool_call/file_modified 事件实时推送
2. **gRPC decompose_task_smart 对齐**: Rust gRPC server 的 decompose 走 Python Orchestrator sandbox 路径
3. **子任务 DAG 可视化**: TUI + Dashboard 展示 depends_on 依赖图
4. **智能重拆分**: 失败子任务触发局部 re-decompose（非简单重试）
5. **Worker 池弹性扩缩**: pending subtasks 数量驱动动态启停 Worker
6. **Checkpoint 持久化到 TiKV**: engine.write_memory → TiKV 后端，跨重启续传

## Technical Approach

1. SandboxManager.execute() stdout 逐行解析 → _publish_event("tool_call"/"file_modified") → NATS → TUI/Dashboard
2. gRPC server SubmitTask → NATS uc.task.submit（已有）+ 移除 Rust 侧 fallback decompose_task
3. TUI: Ink 渲染 ASCII DAG；Dashboard: React DAG 组件
4. handle_subtask_result → failure pattern analysis → re-decompose 局部子任务
5. NatsWorker heartbeat 带 pending_count → 阈值触发 spawn/stop worker
6. Checkpoint: engine.write_memory 已有 TiKV 后端路径，需确保 key_scope="checkpoint" 走 TiKV

## Out of Scope

- 新外部依赖
- proto/API breaking change
