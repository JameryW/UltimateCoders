# Backend: Task Queue for Concurrent submit_task

## Goal

实现并发 submit_task 排队机制：多个任务同时提交时，按顺序逐个发给 Python worker 执行，不丢任务。

## What I already know

* `submit_task_via_bridge` 直接调用 `bridge.send_submit_task()`，没有排队
* Python worker 是单线程顺序执行（`local_worker.py` 的 `_execute_subtasks` 是 for 循环）
* 如果两个 gRPC submit_task 同时到达，两个 `send_submit_task` 会同时写入 stdin，Python worker 可能交错读取导致 JSON 解析错误
* 需要 `mpsc` channel 排队：submit_task 把请求发到 channel，后台 task 顺序消费

## Requirements

* `GrpcServerInner` 新增 `task_queue_tx: mpsc::Sender<QueuedTask>` 和后台消费 task
* `submit_task_via_bridge` 把任务请求发到 queue，立即返回 Planning
* 后台消费 task 从 queue 接收请求，调用 `send_submit_task`，等 notification reader 处理
* 如果 worker 不可用，直接 fallback 到 local decomposition（不走 queue）
- Queue 容量 64，超出返回 error

## Acceptance Criteria

* [ ] 并发 submit_task 排队执行，不丢任务
* [ ] Queue 满时返回 error
* [ ] Worker 不可用时 fallback 不走 queue
* [ ] 测试覆盖

## Out of Scope

* 多 worker 并行（MVP 只一个 worker）
* 任务优先级调度（FIFO 即可）

## Technical Notes

* Key files: `crates/uc-grpc/src/server.rs`
* Pattern: `tokio::sync::mpsc` + `tokio::spawn` 后台消费 loop
