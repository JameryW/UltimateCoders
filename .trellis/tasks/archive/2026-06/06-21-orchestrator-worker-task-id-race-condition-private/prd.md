# 优化 Orchestrator-Worker 交互链路

## Goal

修复 Task ID 不一致、race condition、private 方法调用等正确性问题，并优化 O(n×m) 查找和 broadcast 背压策略。

## What I already know

* **P0 #1**: `orchestrator.py:183` 调用 `engine.submit_task()` 不传 task_id，导致 Rust TaskStore 和 Python Orchestrator 两侧 Task ID 不一致
* **P0 #3**: `nats_worker.py:508-530` 先标记 ASSIGNED 再在 _run_one 中重置为 PENDING，存在 race condition
* **P1 #11**: `orchestrator.py:281` 调用 `sandbox_manager._execute_subprocess()` 绕过了 acquire/release 生命周期
* **P2 #2**: `orchestrator.py:387-393` handle_subtask_result 用 O(n×m) 遍历查找 subtask
* **P2 #7**: `server.rs:764` broadcast::channel(256) 无背压策略，consumer 慢时丢事件

## Requirements

1. **Fix Task ID 一致性**: Orchestrator.submit_task 传 task_id 给 engine.submit_task，确保两侧 ID 一致
2. **Fix race condition**: _execute_subtasks 中不再临时标记 ASSIGNED 再重置，改用 select_next_subtask 本身的去重逻辑
3. **Fix private 方法调用**: decompose_task 使用 SandboxManager 的 public API 而非 _execute_subprocess
4. **Optimize subtask 查找**: 添加 subtask_index 反向索引，O(1) 查找
5. **Add broadcast 背压**: WatchTask stream consumer lag 时触发全量同步而非丢事件

## Acceptance Criteria

- [ ] NATS 提交的任务，gRPC TaskStore 和 Orchestrator.tasks 中 task_id 一致
- [ ] _execute_subtasks 中无 PENDING→ASSIGNED→PENDING 状态翻转
- [ ] decompose_task 不调用任何 `_` 前缀方法
- [ ] handle_subtask_result 查找 subtask 为 O(1)
- [ ] broadcast consumer lag 时 WatchTask stream 发 sync_required 信号而非丢事件
- [ ] 现有测试通过

## Definition of Done

* 所有修改的文件有对应测试覆盖
* cargo test + pytest 通过
* 无新 clippy warning
* 行为变更有注释说明

## Out of Scope

* 双状态源统一（#6，大重构）
* 多 Worker 分布式并行（#8, #9）
* Dashboard Snapshot 改 event-driven（#10）
* TUI 重连状态恢复（#12）
* LocalWorkerBridge 多进程池（#8）

## Technical Notes

* 关键文件: `python/ultimate_coders/agent/orchestrator.py`, `python/ultimate_coders/nats_worker.py`, `crates/uc-grpc/src/server.rs`, `crates/uc-grpc/src/local_worker.rs`
* Engine.submit_task 签名需要扩展支持 task_id 参数
* SandboxManager 需要暴露 public execute_decompose 或类似方法
* broadcast channel 改为 lag detection + sync_required 通知模式
