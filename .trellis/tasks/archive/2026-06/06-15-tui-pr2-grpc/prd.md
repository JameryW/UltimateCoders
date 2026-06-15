# TUI PR2: gRPC 集成 — 扩展 proto + Node.js client

## Goal

扩展 gRPC proto 定义以支持 task/subtask/event 操作，在 uc-grpc-server 中实现新 RPC，并在 TUI 中实现 Node.js gRPC client hook 替换 mock 数据。

## Requirements

### Proto 扩展 (`crates/uc-grpc/proto/engine.proto`)

新增 `TaskService` 包含以下 RPC：
- `SubmitTask(SubmitTaskRequest) → SubmitTaskResponse` — 提交任务
- `GetTask(GetTaskRequest) → GetTaskResponse` — 获取任务状态
- `ListTasks(ListTasksRequest) → ListTasksResponse` — 列出所有任务
- `WatchTask(WatchTaskRequest) → stream TaskEvent` — SSE 式事件流
- `PauseTask(PauseTaskRequest) → PauseTaskResponse` — 暂停任务
- `ResumeTask(ResumeTaskRequest) → ResumeTaskResponse` — 恢复任务

### Rust Server 实现 (`crates/uc-grpc-server`)

- 新增 `TaskServiceImpl` 持有 `Arc<Orchestrator>` 引用
- `SubmitTask` 调用 Orchestrator.submit_task
- `WatchTask` 使用 TaskEventEmitter 的 wait_for_event 循环推送
- 其余 RPC 代理到 Orchestrator 方法

### Node.js gRPC Client (`tui/src/hooks/useGrpcClient.ts`)

- 使用 `@grpc/grpc-js` + `@grpc/proto-loader` 连接 uc-grpc-server
- `useGrpcClient` hook：管理连接状态 + 返回 client 方法
- `useTaskEvents` hook：订阅 WatchTask stream，更新 React state
- 替换 App.tsx 中的 mock 数据

### TUI 真实数据流

- TaskInput 提交 → gRPC SubmitTask → 返回 task + subtask
- WatchTask stream → 实时更新 SubtaskTree 状态
- ChatLog 显示系统消息（来自 TaskEvent）

## Acceptance Criteria

- [ ] proto 新增 TaskService 定义，`cargo check` 通过
- [ ] uc-grpc-server 实现新 RPC，`cargo test` 通过
- [ ] Node.js gRPC client 连接成功
- [ ] TUI 提交 task → 分解 → 子任务执行 → 结果回传 全链路正常
- [ ] SubtaskTree 状态实时更新（通过 WatchTask stream）
- [ ] esbuild 打包成功

## Out of Scope

- Web Dashboard 修改
- Python Orchestrator 逻辑修改
- Textual TUI 修改

## Technical Approach

### Proto 定义

```protobuf
service TaskService {
  rpc SubmitTask(SubmitTaskRequest) returns (SubmitTaskResponse);
  rpc GetTask(GetTaskRequest) returns (GetTaskResponse);
  rpc ListTasks(ListTasksRequest) returns (ListTasksResponse);
  rpc WatchTask(WatchTaskRequest) returns (stream TaskEvent);
  rpc PauseTask(PauseTaskRequest) returns (PauseTaskResponse);
  rpc ResumeTask(ResumeTaskRequest) returns (ResumeTaskResponse);
}
```

### 构建步骤

1. 扩展 proto → `cargo check` 验证编译
2. 实现 Rust server → `cargo test` 验证
3. 编写 Node.js proto loader + client hook
4. 集成到 App.tsx 替换 mock
5. esbuild 打包验证

## Implementation Plan

- **Step 1**: 扩展 proto + Rust 生成代码 + server 实现
- **Step 2**: Node.js gRPC client hook + TUI 集成
- **Step 3**: esbuild 适配 + 端到端验证
