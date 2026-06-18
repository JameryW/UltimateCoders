# Dashboard: gRPC-Web actions, event colors, health fallback

## Goal

让 Dashboard 在 gRPC-Web 连接可用时，任务操作（pause/resume/flush）和 health 查询走 gRPC 路径，同时补全 EventLogPanel 事件颜色。

## Requirements

* gRPC-Web 连接时，pause/resume 走 gRPC PauseTask/ResumeTask RPC
* gRPC-Web 连接时，HealthPanel 用 gRPC Health 塚充数据（SSE 不可用时的 fallback）
* EventLogPanel 补全 subtask/task 事件颜色（task_failed, subtask_started 等）
* 保持 REST fallback（gRPC 不可用时仍走 REST）

## Acceptance Criteria

* [ ] gRPC 连接时点 Pause → 调用 gRPC PauseTask
* [ ] gRPC 连接时点 Resume → 调用 gRPC ResumeTask
* [ ] SSE 不可用 + gRPC 可用时，HealthPanel 显示 gRPC health 数据
* [ ] EventLogPanel 中 task_failed 显示红色，subtask_started 显示蓝色等

## Out of Scope

* gRPC-Web submit（已实现）
* 大规模 UI 重构

## Technical Notes

* useGrpcWeb 已有 submitTask/healthCheck/listTasks
* Proto: PauseTask(PauseTaskRequest) → PauseTaskResponse, ResumeTask(ResumeTaskRequest) → ResumeTaskResponse
* App.tsx handlePauseTask/handleResumeTask 目前走 REST api.pauseTask/resumeTask
