# NatsWorker-Worker模式：分布式 Subtask 调度

## Goal

让 default 模式的 NatsWorker（Orchestrator 角色）在检测到远端 Worker 时，将 subtask 分发到 NATS `uc.subtask.execute` 而非本地执行，实现真正的多 Worker 分布式调度。

## What I already know

* `NatsWorker(mode="worker")` 已能订阅 `uc.subtask.execute` queue group 并执行 subtask
* `_handle_subtask_execute()` 已实现：构建 Subtask → Worker.execute_subtask → publish_update
* default 模式的 `_execute_subtasks()` 目前走本地 `self._worker.execute_subtask()`
* `Orchestrator.workers` dict 存储已注册 Worker 信息（WorkerInfo）
* `OrchestratorDispatcher` 已能通过 NATS request-reply 分解任务并逐层发布 `uc.subtask.execute`
* Worker heartbeat 每 30s 发一次到 `uc.heartbeat`
* Rust gRPC server 的 `mark_tasks_failed_on_worker_death()` 已处理 Worker 死亡检测

## Assumptions (temporary)

* "远端 Worker"的判断标准：NATS 上有非本进程的 Worker 注册（通过 heartbeat 或 registry）
* fallback：无远端 Worker 时仍走本地执行（零配置兼容）

## Requirements

### R1: Worker 发现 — NATS heartbeat 监听

* default 模式 NatsWorker 启动时，额外订阅 `uc.heartbeat` 主题
* 收到 heartbeat → 更新本地 `known_remote_workers: dict[str, RemoteWorkerInfo]`（id, capabilities, load, last_seen）
* 超过 90s 未收到 heartbeat → 从 known_remote_workers 移除
* 当前进程自身的 Worker 不计入（避免自己发给自己）

### R2: 条件性远程分发

* `_execute_subtasks()` 选择执行路径：
  * `known_remote_workers` 非空 → 发布 `uc.subtask.execute` 到 NATS（queue group）
  * `known_remote_workers` 为空 → 本地执行（现有逻辑）
* 远程分发时：publish `uc.subtask.execute` 消息，包含 task_id + subtask_id + description + depends_on + timeout
* 不等回复（fire-and-forget），Worker 完成后通过 `uc.task.update` + `uc.task.event` 通知

### R3: 结果收集

* 远程 Worker 执行完成后，`uc.task.update` 到达 Rust gRPC server → `apply_worker_event_to_store` → broadcast 到 TUI/Dashboard
* default 模式 NatsWorker 也 subscribe `uc.task.event`（已有），收到 subtask_completed/failed 时：
  - 更新 Orchestrator 内部 task state（调用 `orchestrator.handle_subtask_result()`）
  - 触发 `_dispatch_event.set()` 唤醒下一轮调度

## Acceptance Criteria

- [ ] default 模式 NatsWorker 订阅 `uc.heartbeat`，维护 `known_remote_workers` 列表
- [ ] 有远端 Worker 时，subtask 通过 `uc.subtask.execute` 远程分发
- [ ] 无远端 Worker 时，subtask 本地执行（零配置兼容）
- [ ] 远端 Worker 完成后，Orchestrator 收到事件并更新 task state
- [ ] 超时 90s 无 heartbeat 的 Worker 自动移除
- [ ] `cargo test` 通过，现有测试不受影响

## Definition of Done

* Tests added for remote worker discovery logic
* Tests added for conditional dispatch (remote vs local)
* Lint / typecheck green
* 不改 proto，不改 Rust 端

## Out of Scope

* Worker 负载均衡策略（当前用 NATS queue group 随机分配）
* Worker 失败重分配（已有 06-22-worker-failover 任务）
* Worker 能力匹配（当前所有 Worker 接收所有 subtask）
* Worker 认证/授权
* 动态 Worker 缩容保护

## Technical Approach

### R1: Worker 发现

`python/ultimate_coders/nats_worker.py`:
1. `_init_components()` 后，subscribe `uc.heartbeat`
2. `_handle_heartbeat()`: 解析 heartbeat 消息，更新 `known_remote_workers`
3. 新增 `_cleanup_stale_workers()`: 每 60s 检查，移除 90s 无 heartbeat 的 Worker
4. 新增 `known_remote_workers: dict[str, RemoteWorkerInfo]`（dataclass: id, capabilities, load, max_capacity, last_seen）

### R2: 条件性远程分发

`_execute_subtasks()` 修改:
1. `if self._has_remote_workers()` → `_dispatch_remote(subtask)` else `_run_one(subtask_id)`
2. `_dispatch_remote()`: 构建 JSON 消息，publish 到 `uc.subtask.execute`
3. 不等返回 — 结果通过 `uc.task.event` 异步到达

### R3: 结果收集

`_handle_task_event()` 扩展:
1. 收到 `subtask_completed`/`subtask_failed` 时，如果 task_id 在当前执行任务列表中
2. 构建 SubtaskResult → `orchestrator.handle_subtask_result(result)`
3. `_dispatch_event.set()` 唤醒调度循环

## Decision (ADR-lite)

**Context**: 如何判断"有远端 Worker 可用"？
**Decision**: 监听 NATS heartbeat 主题，维护本地 known_remote_workers dict
**Consequences**: heartbeat 是已有的基础设施，零额外配置；但有 90s 延迟检测 Worker 离线

## Technical Notes

* 关键文件: `python/ultimate_coders/nats_worker.py`, `python/ultimate_coders/agent/orchestrator.py`
* NATS subjects: `uc.heartbeat`, `uc.subtask.execute`, `uc.task.event`, `uc.task.update`
* 不改 Rust 端 — 结果收集已通过 `apply_worker_event_to_store` + broadcast 实现
