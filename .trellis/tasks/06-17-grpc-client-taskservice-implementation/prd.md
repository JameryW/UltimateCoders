# gRPC Client TaskService Implementation

## Goal

补全 GrpcEngineClient 的 TaskService 调用能力。

## Requirements

### 1. GrpcEngineClient 添加 TaskServiceClient ✅
- 添加 `task_client: TaskServiceClient<Channel>` 字段
- `connect()` 同时创建两个 client
- 实现 6 个 TaskService 方法

### 2. 补全 conversions ✅
- TaskProto → Task, SubtaskProto → Subtask
- SubmitTaskResponse, GetTaskResponse, PauseTaskResponse, ResumeTaskResponse → Task
- TaskEventProto → AgentEvent
- Helper functions for TaskStatus/SubtaskStatus string→enum mapping

### 3. PyO3 暴露 ❌ 推迟到独立任务
- TaskService 方法不属于 EngineApi trait
- 需要独立设计（扩展 trait 或添加 TaskClient trait）

## Acceptance Criteria

* [x] GrpcEngineClient 可调用全部 6 个 TaskService RPC
* [x] 所有 proto ↔ domain From conversions 存在
* [ ] PyEngine 暴露 task 方法（推迟——需要架构设计）
* [x] cargo check + clippy + test 全绿
* [x] CI green (PR #60)

## Out of Scope

* 将 TaskService 方法加入 EngineApi trait（架构不同）
* Python async generator for watch_task
* 本地 LocalEngine 的 task 方法（已有独立 TaskStore）
