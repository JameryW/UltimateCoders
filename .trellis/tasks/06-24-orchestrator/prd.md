# Orchestrator功能增强

## Goal

在现有 UC Orchestrator OMP extension 基础上，增加任务持久化、上下文传递、取消/暂停/恢复控制（含 subtask 级别），使其从"能跑"进化到"生产可用"。

## Requirements

### R1: 任务持久化 + gRPC 状态同步

* Task state 本地 JSON 文件持久化（`.uc/tasks/<task-id>.json`）
* gRPC 双写：每次状态变更先写本地文件，再异步同步到 gRPC
* 重启恢复：Orchestrator 启动时扫描本地文件，恢复未完成 task
* 修复 `syncTaskToGrpc`：当前每次 submitTask 创建新记录，改为 updateTask（先 get 判断存在性，存在则 update）
* 本地文件为真相源，gRPC 为 dashboard 视图

### R2: 上下文传递 + subtask 间结果共享

* Orchestrator 自动注入已完成 subtask 的结果摘要到后续 worker 的 task prompt
* 摘要控制在 500 字以内，格式：`[已完成 subtask 摘要]`
* 前面 worker 完成后自动把结果写入 uc_memory（scope: task, key: subtask_result_<id>）
* 后续 worker 可通过 uc_memory 工具获取完整细节

### R3: 取消/暂停/恢复控制

* `/uc cancel <task-id>` — 终止运行中 subtask（abort signal），标记 task 为 cancelled
* `/uc cancel <task-id> <subtask-id>` — 取消单个 subtask，跳过它及其下游
* `/uc pause <task-id>` — 等当前 wave 跑完，标记 task 为 paused，不再调度新 wave
* `/uc resume <task-id>` — 从断点继续，调度下一个 wave
* 持久化配合：pause/resume 状态写入本地文件，重启后可恢复
* 取消的 subtask 下游 subtask 自动标记为 cancelled（级联）

## Acceptance Criteria

* [ ] Task state 在进程重启后可恢复（本地 JSON 文件）
* [ ] gRPC sync 正确更新（非每次创建新 task）
* [ ] Worker 执行时 prompt 包含已完成 subtask 摘要
* [ ] Worker 结果自动写入 uc_memory
* [ ] `/uc cancel <task-id>` 可终止运行中的 task
* [ ] `/uc cancel <task-id> <subtask-id>` 可取消单个 subtask 并级联下游
* [ ] `/uc pause <task-id>` 在当前 wave 完成后暂停
* [ ] `/uc resume <task-id>` 从断点继续执行
* [ ] 暂停/取消状态持久化，重启可恢复
* [ ] scheduler.test.ts 更新覆盖新增逻辑

## Definition of Done

* Tests added/updated
* Lint / typecheck green
* Docs/notes updated if behavior changes

## Technical Approach

### 持久化

* `TaskStore` 类：管理 `.uc/tasks/` 目录下的 JSON 文件
* `save(task)` / `load(taskId)` / `loadAll()` / `remove(taskId)`
* Orchestrator 每次状态变更调用 `store.save(task)`
* 启动时 `store.loadAll()` 恢复未完成 task（status in: planning, in_progress, paused）

### gRPC 同步

* 修复 `syncTaskToGrpc`：先 `bridge.getTask()` 判断存在，存在则 update，不存在则 create
* GrpcBridge 新增 `updateTask(taskId, status, subtasks)` 方法
* 异步执行，失败不阻塞

### 上下文注入

* `buildContextForSubtask(def, task)` — 从已完成 subtask 构建摘要
* 注入到 `executeSubtask` 的 task prompt 前缀
* Worker 完成后自动 `bridge.writeMemory("task", "subtask_result_<id>", result)`

### 控制命令

* `AbortController` per task — cancel 时 abort 所有运行中 subtask
* `task.controlState`: "running" | "paused" | "cancelled"
* Wave 循环中检查 controlState，paused 跳出循环，cancelled abort + 标记
* Subtask 级取消：标记 subtask status = cancelled，级联标记下游

### 命令扩展

* `/uc cancel <task-id> [<subtask-id>]`
* `/uc pause <task-id>`
* `/uc resume <task-id>`

## Decision (ADR-lite)

**Context**: Orchestrator 需要从 demo 级别升级到生产可用
**Decision**:
- 持久化：本地 JSON + gRPC 双写（方案 B）
- 上下文：Orchestrator 注入摘要 + uc_memory 自取（方案 C）
- 控制：取消 + 暂停/恢复 + subtask 级控制（方案 C）
**Consequences**: 本地文件保证可靠性，gRPC 挂了不丢数据；摘要注入增加 token 但保证上下文不缺；subtask 级控制增加复杂度但提供细粒度管理

## Out of Scope

* 实时 SSE/WebSocket 进度推送（下一个 task）
* Worker 超时自动降级
* 多用户/多项目隔离
* gRPC stream watch

## Technical Notes

* orchestrator.ts: ~570 lines, 核心逻辑
* scheduler.ts: ~130 lines, DAG + cycle detection
* grpc-bridge.ts: ~250 lines, 需新增 updateTask 方法
* extension.ts: ~200 lines, 需新增命令路由
* 本地存储路径: `.uc/tasks/` (gitignore)
