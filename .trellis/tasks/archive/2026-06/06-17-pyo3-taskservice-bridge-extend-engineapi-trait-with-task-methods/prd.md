# PyO3 TaskService Bridge: Extend EngineApi with Task Methods

## Goal

将 TaskService 的 5 个核心方法（submit_task, get_task, list_tasks, pause_task, resume_task）加入 EngineApi trait，使 LocalEngine 和 GrpcEngineClient 统一实现，然后通过 PyO3 暴露给 Python。

## Architecture Decision

**方案 A（选择）**: 扩展 EngineApi trait
- Task 方法加入 trait，LocalEngine 用内部 TaskStore 实现，GrpcEngineClient 用 gRPC 调用
- 最统一，Python 层无需区分 local/grpc 模式
- 代价：改动 EngineApi trait + 所有 impl

**方案 B（排除）**: PyEngine 持有额外 Arc<dyn TaskApi>
- 多一个 trait，多一个 trait object，PyEngine 构造更复杂

**方案 C（排除）**: PyEngine 直接持有 Arc<GrpcEngineClient>
- 破坏抽象，local 模式无法使用

## Requirements

### 1. EngineApi trait 扩展
在 uc-types/src/engine.rs 中添加 5 个方法：
```rust
async fn submit_task(&self, description: String, project_id: String) -> Result<Task, EngineError>;
async fn get_task(&self, task_id: &str) -> Result<Task, EngineError>;
async fn list_tasks(&self) -> Result<Vec<Task>, EngineError>;
async fn pause_task(&self, task_id: &str) -> Result<Task, EngineError>;
async fn resume_task(&self, task_id: &str) -> Result<Task, EngineError>;
```

注意：`watch_task` 不加入 trait（stream 返回类型复杂，且 Python 端用 collect 模式即可）

### 2. LocalEngine 实现
- LocalEngine 添加 `task_store: Arc<Mutex<TaskStore>>` 字段
- TaskStore 从 uc-grpc 提取到 uc-engine（或 uc-types）
- 实现 5 个方法，委托给 TaskStore

### 3. GrpcEngineClient 实现
- 已有 inherent impl，改为 trait impl 即可
- 已有 From conversions

### 4. gRPC server 更新
- server.rs 的 TaskService impl 继续使用 inner TaskStore
- 无需改动（server 直接调 engine.task_store 或 engine.submit_task）

### 5. PyO3 暴露
- PyEngine 添加 sync + async 方法：
  - `submit_task(description, project_id=None)`
  - `get_task(task_id)`
  - `list_tasks()`
  - `pause_task(task_id)`
  - `resume_task(task_id)`

## Acceptance Criteria

* [x] EngineApi trait 包含 5 个 task 方法
* [x] LocalEngine 实现全部 5 个方法
* [x] GrpcEngineClient 实现全部 5 个方法（从 inherent 移到 trait）
* [x] PyEngine 暴露全部 5 个方法（sync + async）
* [x] cargo check + clippy + test 全绿

## Out of Scope

* watch_task（server-streaming，暂不加入 trait）
* TaskStore 从 uc-grpc 提取到独立 crate（直接在 uc-engine 中创建简化版）
* Python async generator
