# T8 勘察记录 (2026-09-14)

## 现状

- `execution_graphs.project_id TEXT NOT NULL DEFAULT ''` + `idx_execution_graphs_project`
  （T4 已建，graph_store.rs 迁移段）；upsert 两条 SQL（shadow upsert ON CONFLICT
  DO UPDATE 携 project_id；backfill DO NOTHING）。
- `Task.project_id` 已是类型字段；`NatsSubtaskExecute.project_id` 已在 wire 上
  （T1），`subtask_execute_payload(task_id, st, project_id, ...)` 已携带。
- `publish_ready_subtasks(task_id)` / `dispatch_ready_subtasks(...)` 都已把
  `project_id` 从 task 取出（现只透传给 payload）——`dispatch_gate` 调用点就在
  同一作用域，加参即可。
- `WorkerRegistry.register(worker_id, capabilities, max_capacity, metadata,
  contract_version)`；`RegisteredWorker` 无 scope 字段。`dispatch_gate(required)`
  返回三态 Dispatch / NoCapableWorker / NoVersionMatchedWorker。
- `RegisterWorkerRequest` 字段 1..5 已占用 → projects = 6。
- 注册链路：proto → worker_service.rs handler → registry.register；
  Rust client.rs `register_worker(...)` → uc-python engine.rs `register_worker_async`
  (pyo3, signature `(worker_id, capabilities, max_capacity, metadata=None,
  contract_version=None)`) → python engine.py 包装 → nats_worker.py
  `_register_with_gateway`（worker_id/capabilities/max_capacity 取自
  Worker.get_info()）。
- uc.task.submit 消费：nats_worker.py `_handle_submit`（default/orchestrator 模式），
  现只校验 description 非空。
- scheduler 路径：scheduler_dispatch.rs `build_payload(task)` 直接带
  task.project_id 发布（未校验）。
- EngineError 变体用 `TaskError(String)`（既有用法）。

## 裁决

- scope 匹配语义：`serves_scope(p) = worker.projects.is_empty() || p ∈ projects`。
  注册时归一化 projects（trim、丢空串、去重）→ scoped worker 不可能声明 ""，
  空 scope 任务只匹配 open worker。
- gate 顺序：capability（现有）→ scope（新，hard）→ contract_version（现有）。
  空 registry + 空 caps 保持 best-effort Dispatch（NATS-only 部署兼容，不变）。
  新变体 `NoScopeMatchedWorker { workers: Vec<String> }`（列 capability 匹配但
  scope 不符的 worker ids）。
- 心跳不带 projects：scope 只在注册时声明，改 scope = 重注册（现有 upsert 语义）。
- 不可变：gateway 侧 update 路径（NatsTaskUpdateEnvelope.project_id 仅用于
  restart recovery——已存在的 task 不回写 scope；submit 后 project_id 只读）。
- graph 校验：纯函数 `validate_graph_scope(&str) -> Result<(), EngineError>`
  （trim 非空），`upsert_task_shadow` 接入；backfill 不动。
