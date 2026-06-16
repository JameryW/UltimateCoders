# TUI End-to-End Task Execution — Local Execution Path

## Goal

让 TUI 提交的任务能真正被执行（不只是换行拆分假执行），无需依赖 NATS + 独立 Python worker 进程。

## Requirements

* TUI submit task 后，task 必须被真正分解和执行（不是假执行）
* 不需要 NATS / Docker / 额外进程
* 执行状态通过 WatchTask stream 实时更新到 TUI
* 兼容现有 NATS 路径（NATS 可用时仍走 NATS）
* Python worker 不可用时静默降级到换行拆分，TUI 状态栏显示 "local execution unavailable"

## Acceptance Criteria

* [ ] TUI 连 gRPC server，提交 task，能在 TUI 里看到 subtask 从 assigned → in_progress → completed
* [ ] 不启动 NATS / nats_worker.py，任务仍可执行
* [ ] 执行结果写入 memory（通过 EngineApi）
* [ ] NATS 可用时仍走 NATS 路径（不破坏已有功能）
* [ ] Python 不可用时降级到换行拆分，TUI 状态栏显示不可用
* [ ] Worker 崩溃后自动重启

## Definition of Done

* Tests added/updated (unit/integration where appropriate)
* Lint / typecheck / CI green
* Docs/notes updated if behavior changes

## Decision (ADR-lite)

**Context**: TUI 提交任务后，gRPC server 的 `submit_task_local` 只做换行拆分不执行，需要真正的执行路径。
**Decision**: 方案 A — gRPC server spawn Python 子进程（`local_worker.py`），通过 JSON-RPC 2.0 over stdin/stdout 通信。
**Consequences**: 不需要 NATS/Docker，Python 生态完整可用，子进程隔离；需要用户装 Python + maturin。

### 子决策

* **进程生命周期**: 常驻进程复用（gRPC server 启动时 spawn，多 task 复用）
* **通信协议**: JSON-RPC 2.0 over stdin/stdout（newline-delimited）
* **降级策略**: 混合 — worker 启动失败 → 换行拆分 + TUI 状态栏显示 "local execution unavailable"

## Out of Scope

* 多 worker 并行（当前单 worker 顺序执行，与 nats_worker 一致）
* PyO3 内嵌（GIL + async FFI 问题太多，以后再考虑）
* 修改 TUI 代码（TUI 只需读 gRPC 返回的数据，不需要改）

## Technical Approach

### 架构

```
gRPC server (Rust)
  ├─ NATS 可用? → publish uc.task.submit (现有路径)
  └─ NATS 不可用?
      ├─ LocalWorker 可用? → JSON-RPC → local_worker.py → Orchestrator
      └─ LocalWorker 不可用? → 换行拆分 (现有降级)
```

### Rust 端 (uc-grpc)

1. **`LocalWorkerBridge`** — 管理 Python 子进程生命周期
   - `new()` — spawn `python -m ultimate_coders.local_worker`，初始化 JSON-RPC
   - `submit_task(description, project_id)` — 发 JSON-RPC request，等 response
   - `is_available()` — worker 是否健康
   - 崩溃检测 + 自动重启（监控 stdout stderr，进程 exit 时重启）

2. **`GrpcServerInner`** 新增 `local_worker: Option<LocalWorkerBridge>` 字段

3. **`submit_task`** 修改：
   - NATS 可用 → 走 NATS (不变)
   - NATS 不可用 + local_worker 可用 → JSON-RPC submit
   - NATS 不可用 + local_worker 不可用 → 换行拆分 (不变)

4. **Worker 状态暴露**：在 `HealthResponse` 中新增 `local_worker: ComponentHealthProto`

### Python 端 (ultimate_coders)

1. **`local_worker.py`** — 新模块，入口 `python -m ultimate_coders.local_worker`
   - 从 stdin 读 JSON-RPC 2.0 messages
   - 处理 `submit_task` method → 调 `Orchestrator.submit_task()`
   - 通过 JSON-RPC notification 推送进度（subtask_started, subtask_completed 等）
   - 结果写回 stdout
   - stderr 留给日志

### JSON-RPC Protocol

```jsonc
// Server → Worker: submit_task
{"jsonrpc": "2.0", "id": 1, "method": "submit_task", "params": {"description": "...", "project_id": "..."}}

// Worker → Server: progress notification
{"jsonrpc": "2.0", "method": "task_update", "params": {"task_id": "...", "status": "in_progress", "subtasks": [...]}}

// Worker → Server: completion response
{"jsonrpc": "2.0", "id": 1, "result": {"task_id": "...", "status": "completed", "subtasks": [...]}}

// Server → Worker: health check (ping)
{"jsonrpc": "2.0", "id": 0, "method": "ping"}

// Worker → Server: health check response
{"jsonrpc": "2.0", "id": 0, "result": {"status": "ok"}}
```

### 关键设计

* **newline-delimited JSON**: 每个 JSON-RPC message一行，以 `\n` 分隔
* **Rust 端用 `tokio::process::Command`** + `BufReader<ChildStdout>` / `BufWriter<ChildStdin>`
* **进度推送**: worker 发 notification → Rust 端收到后更新 TaskStore → WatchTask stream 自动推给 TUI
* **超时**: submit_task 的 JSON-RPC response 超时由 Orchestrator 内部控制，Rust 端不设额外超时

## Technical Notes

### Key files
* `crates/uc-grpc/src/server.rs` — GrpcServer, submit_task, submit_task_local, GrpcServerInner
* `crates/uc-grpc-server/src/main.rs` — standalone server binary
* `python/ultimate_coders/nats_worker.py` — NATS consumer (参考模式)
* `python/ultimate_coders/agent/orchestrator.py` — Orchestrator (被 local_worker 调用)
* `tui/src/grpc/client.ts` — TS gRPC client (不需改)
* `tui/src/hooks/useGrpcClient.ts` — React hook (不需改)
* `tui/src/statusbar-utils.ts` — 状态栏显示逻辑 (可能加 local worker 状态)

### 现有模式参考
* `nats_worker.py` 的 NatsWorker 类 — 类似的 worker 模式，用 Orchestrator
* `GrpcServer::with_nats()` — 类似的外部服务初始化模式
* `TaskStore` — 内存 store，已支持 events 和 WatchTask stream
