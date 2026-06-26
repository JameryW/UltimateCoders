# Add Worker Status Awareness to OMP Orchestrator

## Goal

让 OMP 的 LLM Agent 能感知 Worker 运行状况——通过新增 `uc_worker` 工具，Agent 可查询 worker 列表、负载、能力、心跳状态，从而做出更智能的调度和诊断决策。

## What I already know

* **Rust gRPC 已有 `ListWorkers` RPC** — 在 `DashboardService` 中实现，但只走 NATS 路径，OMP 的 `GrpcBridge` 没有调用入口
* **`Health` RPC 返回 `local_worker` 组件状态** — 只有 healthy/unavailable 二值，无负载/能力详情（`server.rs:2164`）
* **Python Worker 已有 `send_heartbeat()`** — 返回 `{worker_id, capabilities, current_load, max_capacity}`
* **Python Worker 已有 `get_info()`** — 返回 `WorkerInfo(id, capabilities, current_load, max_capacity, last_heartbeat)`
* **`WorkerProto` 在 proto 中已定义** — `id, capabilities, current_load, max_capacity, load_percent, last_heartbeat, heartbeat_age_seconds, heartbeat_stale, is_available`
* **Dashboard 的 `WorkersPanel` 已渲染 worker 数据** — OMP 的 agent 层完全没接入
* **OMP tool 注册模式清晰** — `registerTaskTools`, `registerMemoryTools`, `registerFileTools` 三个 bridge 文件，每个注册一个 `uc_*` 工具

## Assumptions (temporary)

* MVP 不需要修改 proto 定义（`ListWorkers` RPC 和 `WorkerProto` 已经存在）
* MVP 不需要 worker 注册/心跳机制（已存在），只需要暴露查询入口
* `GrpcBridge.rpc()` 的 JSON-over-HTTP 模式可以直接调用 `ListWorkers` RPC

## Decision (ADR-lite)

**Context**: OMP agent 需要 worker 运行状况信息，`ListWorkers` RPC 已存在于 `DashboardService`，但 `GrpcBridge` 无调用入口。
**Decision**: 方案 A — `GrpcBridge` 新增 `listWorkers()` 调用 `DashboardService/ListWorkers` RPC，`resolveService` 加 `DashboardService` 映射。NATS 不可用时降级到 `Health` RPC 的 `local_worker` 组件状态。
**Consequences**: 依赖 NATS 连通才能返回完整 worker 列表；纯 local 模式（无 NATS）只能拿到 local_worker 的 healthy/unavailable 状态。

## Open Questions

(none remaining)

## Requirements

* 新增 `uc_worker` LLM tool，注册到 OMP ExtensionAPI
* `uc_worker` 支持 `list` action：返回所有 worker 的状态摘要
* `uc_worker` 支持 `status` action（可选）：返回单个 worker 详情
* `GrpcBridge` 新增 `listWorkers()` 方法，调用 gRPC `ListWorkers` RPC
* 在 `extension.ts` 中调用注册函数

## Acceptance Criteria

* [ ] `uc_worker` tool 注册成功，LLM agent 可调用
* [ ] `uc_worker list` 返回 worker 列表（id, load, capacity, available, heartbeat age）
* [ ] gRPC server 不可用时优雅降级（返回 empty/unavailable）
* [ ] `resolveService` 正确路由 `ListWorkers` 到对应 service

## Definition of Done

* Lint / typecheck 绿
* 手动测试通过（`./run-omp.sh --server` 启动后 agent 可调用 `uc_worker`）
* 与现有 tool bridge 文件风格一致

## Out of Scope

* 修改 proto 定义
* Worker 注册/心跳机制（已存在）
* Dashboard 前端改动
* Worker 事件推送/streaming（watch worker）

## Technical Notes

### 关键文件

* `packages/uc-orchestrator/src/orchestrator/grpc-bridge.ts` — 需加 `listWorkers()` + 更新 `resolveService()`
* `packages/uc-orchestrator/src/orchestrator/worker-bridge.ts` — **新建**，注册 `uc_worker` tool
* `packages/uc-orchestrator/src/extension.ts` — import + 调用 `registerWorkerTools()`
* `crates/uc-grpc/src/dashboard_service.rs` — `ListWorkers` RPC 实现所在
* `crates/uc-grpc/src/server.rs:2164` — `health()` 方法（参考模式）

### proto 中的 service 归属

已确认：`ListWorkers` 属于 `DashboardService`（走 NATS passthrough 到 Python Orchestrator）。`GrpcBridge.resolveService()` 需新增 `DashboardService` 映射。

### 降级策略

NATS 不可用时 `ListWorkers` 返回 `available: false, workers: []`。此时降级到 `Health` RPC 的 `local_worker` component，构造一个精简 worker 条目（仅 id="local_worker", status=healthy/unavailable）。
