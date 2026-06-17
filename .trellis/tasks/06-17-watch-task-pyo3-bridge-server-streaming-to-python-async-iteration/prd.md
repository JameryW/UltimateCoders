# watch_task PyO3 Bridge: Server-Streaming to Python Async Iteration

## Goal

将 gRPC server-streaming `WatchTask` RPC 通过 PyO3 桥接到 Python，使 Python 用户可以 `async for event in engine.watch_task(task_id)` 实时接收任务事件。

## Background

- gRPC server 的 `watch_task` 返回 server-streaming RPC（hybrid: replay + live broadcast）
- GrpcEngineClient 有 `watch_task` inherent method 返回 `Pin<Box<dyn Stream<Item = AgentEvent> + Send>>`
- watch_task 不在 EngineApi trait 中（stream 返回类型不适合统一 trait）
- LocalEngine 没有对应的 stream 实现（无 broadcast channel）

## Architecture Decision

**PyO3 async generator 模式**：使用 `pyo3_async_runtimes` + `async gen` 让 Python 可以 `async for` 迭代。

但 PyO3 不原生支持 async generator。有两种可行方案：

**A. Callback 模式** — `watch_task(task_id, callback)` 每个 event 调 callback
- 简单，但不是 Pythonic

**B. Collect-then-return 模式** — `watch_task(task_id, max_events=100, timeout_secs=30)` 收集一批后返回
- 最简单，不依赖 async generator
- 用户可循环调用来持续获取

**C. AsyncIterator via __aiter__/__anext__** — 创建 `PyEventStream` wrapper 实现 Python async iteration protocol
- 最 Pythonic，但实现复杂度高（需要 PyO3 class + async runtime 桥接）

**选择 B**（collect 模式）— 最实用且最简单。watch_task 主要用于 dashboard 更新，collect 模式配合 polling 完全够用。如果未来需要 true streaming，可以升级到方案 C。

## Requirements

### 1. GrpcEngineClient watch_task（inherent，已有）
- 确认 `watch_task` 方法在 client.rs 中存在
- 返回 `Result<Pin<Box<dyn Stream<Item = AgentEvent> + Send>>, EngineError>`

### 2. PyO3 watch_task 方法
在 PyEngine 上添加两个方法：

```python
# Python 接口
engine.watch_task(task_id, max_events=50, timeout_secs=10.0)  # -> list[PyAgentEvent]
engine.watch_task_async(task_id, max_events=50, timeout_secs=10.0)  # -> coroutine -> list[PyAgentEvent]
```

Rust 实现逻辑：
1. 调用 `inner.watch_task(task_id)` 获取 stream
2. 用 `tokio::time::timeout` 包装
3. 在 timeout 内收集最多 `max_events` 个 event
4. 返回 `Vec<PyAgentEvent>`

注意：watch_task 不是 EngineApi trait 方法，所以 PyEngine 需要直接访问 GrpcEngineClient。
由于 `inner` 是 `Arc<dyn EngineApi>`，无法调用 inherent 方法。

**解决方案**: 添加一个可选的 `grpc_client: Option<Arc<GrpcEngineClient>>` 字段到 PyEngine。
在 gRPC 模式下同时存储 `inner`（trait object）和 `grpc_client`（具体类型）。
Local 模式下 `grpc_client` 为 None，watch_task 返回错误。

### 3. PyAgentEvent 类型
创建 `PyAgentEvent` wrapper：
```rust
#[pyclass]
#[derive(Clone)]
pub struct PyAgentEvent {
    pub event_type: String,
    pub task_id: String,
    pub subtask_id: Option<String>,
    pub data: String,  // JSON string
    pub timestamp: String,
}
```

### 4. Python Engine wrapper
在 engine.py 中添加：
```python
def watch_task(self, task_id: str, max_events: int = 50, timeout_secs: float = 10.0) -> list:
    """Watch a task for events. Returns a batch of events."""
    return self._try_grpc_with_fallback("watch_task", task_id, max_events, timeout_secs)
```

## Acceptance Criteria

* [ ] PyEngine 有 watch_task 和 watch_task_async 方法
* [ ] gRPC 模式下可收集事件（带 timeout + max_events）
* [ ] local 模式下返回明确错误（"watch_task requires gRPC mode"）
* [ ] PyAgentEvent 类型可用
* [ ] Python Engine.watch_task 方法可用
* [ ] cargo check + clippy + test 全绿

## Out of Scope

* True Python async generator (方案 C)
* LocalEngine 的 watch_task 实现（没有 broadcast channel）
* EngineApi trait 中添加 watch_task
