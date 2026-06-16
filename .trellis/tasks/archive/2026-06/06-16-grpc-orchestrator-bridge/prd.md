# gRPC-Orchestrator Bridge

## Goal

打通 TUI → gRPC Server → Python Orchestrator 全链路，让 TUI 提交的任务被真正的 Orchestrator（LLM 分解 + Worker 执行）处理，而不是当前的空壳 TaskStore（按换行拆分，无执行）。

## What I already know

* gRPC server `GrpcServerInner` 只有 `engine: E` + `task_store: Arc<Mutex<TaskStore>>`，不连接 Python Orchestrator
* `TaskStore::submit_task()` 只做简单换行拆分分解，subtask 永远停在 Pending（无 Worker 执行）
* Python `Orchestrator` 有完整的 LLM/Sandbox 分解 + Worker 分配 + 结果聚合逻辑
* NATS 依赖已就位：`async-nats = "0.38"` 在 workspace，`messaging` feature 已定义，docker-compose 有 NATS 服务
* Rust 端 `OrchestratorDispatcher` 在 `messaging` feature 下有 NATS publish 实现
* Python 端没有 NATS consumer — Orchestrator 是被 Dashboard/TUI 直接 await 调用的
* TUI 通过 `useGrpcClient` hook 调用 `submitTask`/`getTask`/`listTasks`/`pauseTask`/`resumeTask`
* Dashboard FastAPI 有 `submit_task`/`pause_task`/`resume_task` 端点，直接调 Orchestrator

## Decisions

### D1: NATS 双向桥接

- **Context**: gRPC server (Rust) 和 Python Orchestrator 运行在不同进程，需要跨进程通信
- **Decision**: 使用 NATS JetStream 作为消息总线
  - Rust gRPC server → NATS publish (`uc.task.submit`) → Python consumer
  - Python → NATS publish (`uc.task.update`, `uc.task.event`) → Rust subscriber
- **Consequences**: 解耦 Rust/Python；支持多 Orchestrator 水平扩展；需要 NATS 基础设施运行

### D2: 独立 Python NATS consumer 进程

- **Context**: NATS consumer 需要长期运行的 async event loop，且需要独立的 Orchestrator/Worker/Engine 实例
- **Decision**: 新建 `python -m ultimate_coders.nats_worker` 入口，独立进程运行 NATS consumer + Orchestrator + Worker
- **Consequences**: 与 Dashboard 进程完全解耦；需要独立初始化 Engine/LLM client；需要进程管理（supervisor/docker）

### D3: Python 推送状态、gRPC 订阅更新 TaskStore

- **Context**: TUI 查询任务状态来自 gRPC TaskStore（内存 HashMap），Python Orchestrator 是执行者
- **Decision**: Python 每次状态变更通过 NATS `uc.task.update` 推送，gRPC server 订阅并更新内存 TaskStore
- **Consequences**: TUI 查询始终最新；无需轮询；Python 侧需要在每个状态变更点 emit NATS 消息

### D4: Dashboard submit_task 也走 NATS

- **Context**: Dashboard 目前直接调 `orch.submit_task()`，绕过 gRPC TaskStore，状态不同步
- **Decision**: Dashboard `submit_task` 改为通过 NATS publish，由独立 consumer 进程处理。Dashboard 状态查询改为从 gRPC TaskStore 或 NATS 订阅获取
- **Consequences**: 统一任务入口；Dashboard 不再直接持有 Orchestrator 引用；Dashboard 状态展示需要适配

### D5: NATS 不可用时 graceful degradation

- **Context**: NATS 可能未启动或临时不可用
- **Decision**: gRPC server 在 NATS publish 失败时回退到当前换行拆分模式（TaskStore 本地分解），TUI 不完全失效
- **Consequences**: 用户始终能提交任务；任务质量降低（无 LLM 分解）但不会报错

### D6: 心跳 + 任务超时

- **Context**: Python consumer 可能崩溃，任务卡在 InProgress
- **Decision**: Python consumer 定期发心跳到 NATS (`uc.heartbeat`)，gRPC server 监控心跳。任务超过配置超时（默认 10 分钟）无更新时标记为 Failed
- **Consequences**: 防止任务永远卡住；需要可配置超时时间

## Requirements

### AC1: NATS 消息协议

- 定义 NATS subject 结构：`uc.task.submit` / `uc.task.update` / `uc.task.event` / `uc.heartbeat`
- 定义消息 payload 格式（JSON）：submit 请求、update 状态变更、event 实时事件
- Rust 和 Python 共享相同的 subject + payload 格式

### AC2: Rust gRPC server NATS 集成

- `GrpcServerInner` 新增 `nats_client: Option<async_nats::Client>` 字段
- `submit_task()` 优先 NATS publish，失败时回退 TaskStore 本地分解
- 新增 NATS subscriber 协程，订阅 `uc.task.update` 和 `uc.task.event`，更新 TaskStore
- 新增心跳监控：定期检查 `uc.heartbeat`，超时标记任务 Failed

### AC3: Python NATS consumer 进程

- 新建 `python/ultimate_coders/nats_worker.py` 模块
- 入口 `python -m ultimate_coders.nats_worker`
- 初始化：Engine + Orchestrator + Worker + NATS connection
- 订阅 `uc.task.submit`，调用 `orchestrator.submit_task()`
- Orchestrator 状态变更时 publish `uc.task.update` 和 `uc.task.event`
- 定期 publish 心跳到 `uc.heartbeat`

### AC4: Orchestrator NATS event hook

- `Orchestrator` 新增 `nats_publisher` 可选依赖
- 在 `submit_task` / `assign_subtask` / `handle_subtask_result` 等状态变更点 emit NATS 消息
- 复用 `TaskEventEmitter` 的 event 数据格式

### AC5: Dashboard 适配

- Dashboard `submit_task` 改为 NATS publish（不再直接调 Orchestrator）
- Dashboard 状态展示改为从 NATS 订阅或 gRPC TaskStore 查询
- Dashboard 保持独立 SSE 端点（可从 NATS `uc.task.event` 消费）

### AC6: 单元测试

- Rust: TaskStore NATS 更新逻辑（mock NATS 消息）
- Rust: graceful degradation（NATS 不可用时回退本地分解）
- Rust: 心跳超时检测
- Python: NATS consumer 消息解析 + Orchestrator 调用
- Python: NATS publisher 在状态变更点 emit 正确消息

## Acceptance Criteria

- [ ] TUI submitTask → NATS → Python consumer 收到 → LLM 分解 → Worker 执行
- [ ] TUI getTask 返回真实状态（Pending → Assigned → InProgress → Completed）
- [ ] 任务完成后 TUI getTask 返回 result
- [ ] `python -m ultimate_coders.nats_worker` 可独立启动
- [ ] NATS 不可用时 TUI submitTask 仍能工作（回退换行拆分）
- [ ] Python consumer 崩溃后，超时任务标记为 Failed
- [ ] Dashboard submit_task 通过 NATS 提交，状态与 TUI 一致
- [ ] `cargo test -p uc-engine` 和 `pytest` 通过
- [ ] `cargo clippy` + `cargo fmt --check` clean

## Definition of Done

* Tests added/updated（Rust + Python）
* Lint / typecheck / CI green
* Docs/notes updated（NATS 消息协议文档）
* Backward compatible — 不破坏现有 TUI / Dashboard 功能

## Out of Scope

* Dashboard 前端改动
* Proto 定义大改（新增字段 OK）
* 多 Orchestrator 实例 / 负载均衡
* 任务持久化到数据库（仍在内存 TaskStore）
* Worker 自动注册（手动注册即可）
* gRPC server 嵌入 Python
* Docker Compose 新增 nats-worker 服务（手动启动即可）

## Implementation Plan

### PR1: NATS 消息协议 + Rust gRPC server 集成

1. 定义 NATS subject + payload JSON schema
2. `GrpcServerInner` 新增 `nats_client` + subscriber 协程
3. `submit_task()` NATS publish + graceful degradation
4. `TaskStore` 新增 `apply_update()` 方法（从 NATS 消息更新状态）
5. 心跳监控 + 任务超时检测
6. 测试：graceful degradation、TaskStore 更新、超时检测

### PR2: Python NATS consumer + publisher

1. 新建 `python/ultimate_coders/nats_worker.py`
2. NATS consumer：订阅 `uc.task.submit`，调用 Orchestrator
3. NATS publisher：Orchestrator 状态变更时 publish `uc.task.update` / `uc.task.event`
4. 心跳 publish
5. 入口脚本 `python -m ultimate_coders.nats_worker`
6. 测试：consumer 消息解析、publisher emit

### PR3: Dashboard 适配 + 集成测试

1. Dashboard `submit_task` 改为 NATS publish
2. Dashboard 状态展示从 NATS/gRPC TaskStore 获取
3. 端到端测试：TUI submit → NATS → Python → NATS → gRPC → TUI 查询
4. 文档更新

## Technical Notes

* 关键文件：
  - `crates/uc-grpc/src/server.rs` — gRPC server + TaskStore
  - `crates/uc-engine/src/scheduler/dispatcher.rs` — NATS OrchestratorDispatcher
  - `python/ultimate_coders/agent/orchestrator.py` — Python Orchestrator
  - `python/ultimate_coders/agent/worker.py` — Python Worker
  - `python/ultimate_coders/engine.py` — Python Engine (初始化 LLM/存储)
  - `python/ultimate_coders/dashboard/app.py` — Dashboard FastAPI
  - `tui/src/hooks/useGrpcClient.ts` — TUI gRPC client hook
* NATS subject 设计：
  - `uc.task.submit` — gRPC/Dashboard → Python（新任务）
  - `uc.task.update` — Python → gRPC（状态更新：subtask assigned/completed/failed, task completed）
  - `uc.task.event` — Python → gRPC（实时事件：tool_call, llm_request 等）
  - `uc.heartbeat` — Python → gRPC（consumer 心跳）
* Payload 格式（JSON）：
  - submit: `{"task_id": "...", "description": "...", "project_id": "..."}`
  - update: `{"task_id": "...", "status": "...", "subtasks": [...], "result": "..."}`
  - event: `{"type": "...", "task_id": "...", "subtask_id": "...", "data": {...}}`
  - heartbeat: `{"consumer_id": "...", "timestamp": "..."}`
* Python `nats-py` 库可用于 NATS consumer
* Rust 端已有 `OrchestratorDispatcher` 可复用 NATS publish 逻辑
